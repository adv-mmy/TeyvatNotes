# 离线理解、答案评测与可靠性演示

日期：2026-10-04。对应学习建议 13.11 第 6 项的离线基础，不代表真实攻略 Agent 已通过完整验收。

## 运行与结果

在产品仓库根目录执行，不需要 `.env`、PostgreSQL、模型凭据或网络：

```powershell
uv run python -m teyvatnotes.knowledge.evaluation_cli demo
uv run python -m teyvatnotes.knowledge.evaluation_cli reliability
```

`demo` 输出私有 JSON 评测报告；当前 `status=completed_with_findings`，50 题中 45 题流程通过、5 题失败，退出码 **1**。这不是程序崩溃，而是评分器检测到了问题：5 道冲突题的工具搜索已命中两篇相互矛盾的虚构资料，但现有离线模拟器仅回读其中一篇并给出答案，因此 `expected_status` 和 `reference_read` 不通过，回读参考召回率为 0.5。不能通过修改参考答案或删题把报告变成全绿；也不能由模拟器结果推断真实模型一定犯同样的错误。

`reliability` 输出 9 项故障注入检查；当前均通过，`status=ok`，退出码 **0**。两条命令均无真实 API 调用。评测记录的 105 次生成尝试是模拟次数，不是 105 次付费请求。

报告只输出到标准输出，不自动保存、覆盖文件或接入中央日志。完整报告包含玩家输入、最终答案、引用和校验通过的工具参数，是私有调试资料；分享前需脱敏。不包含原始非法参数、opaque reasoning、服务端异常原文或凭据。

## 固定语料与可重复性

| 类别 | 问题数 | 参考与预期 |
| --- | --- | --- |
| 角色 | 15 | 标注装备、操作、适用条件对应的块 |
| 任务 | 15 | 标注命中块以及前置、步骤、完成标志四块 |
| 条件不足 | 10 | 追问，零模型/工具尝试 |
| 资料冲突 | 5 | 两篇相反资料，应表达证据不足而非直接回答 |
| 范围之外 | 5 | 证据不足，零模型/工具尝试 |

按类别划分 development 30 题、holdout 20 题。两组共用四份虚构快照、九个知识块，很多题是同一事实的不同表达；这不是 50 条独立真实事实，也不是真正盲测的游戏质量验收集。`run_evaluation()` 固定使用离线模拟器，不提供真实客户端注入或付费命令分支。

人工参考采用来源 ID＋分块 ordinal，不能依据当前检索排名自动反推“正确答案”。导入前校验所有快照的规范化数据哈希，漂移返回 `evaluation_fixture_drift`；参考缺失在生成前返回 `evaluation_reference_missing`。需要有意更新语料时，应同时审查参考标注、修订号和固定哈希，而非静默接受新内容。

报告记录语料修订 `fictional-guide-v1`、问题清单哈希、资料哈希、提示词哈希、输出 Schema 哈希、处理版本和模拟器身份。固定输入和参考可复现；耗时不是固定值。两个新冲突样本保留在 `samples/guides/`，明确标注虚构与 DEMO-ONLY，不可发布到正式资料。

## 评分维度与边界

- `tool_search_reference_recall`：实际模型工具搜索的参考命中率，不把问答预检当成工具命中。
- `first_tool_search_mrr`：第一次工具搜索中首个参考证据的倒数排名；无参考时为 `null`。
- `read_reference_recall`：成功回读账本中的参考覆盖率；搜索命中不等于模型实际读过。
- 检查预期状态、输出结构、引用 ID 是否来自成功回读、资料版本/实体/条件、块与文档对应关系、显式装备/角色是否属于玩家持有集合、上下文截断、标注步骤引用覆盖与顺序、任务三类 section 的存在与结构顺序、调用预算和零调用负例。
- 装备/角色只检查模型显式声明的 `required_*`，不能识别自然语言中遗漏的资源前提。步骤检查只对应标注块和 section，不证明真实任务的所有步骤完整或可执行。
- `semantic_citation_support`、`game_fact_correctness` 均为 `null`，每题 `manual_semantic_review_required=true`；汇总始终 `publication_ready=false`、`semantic_quality_verified=false`。引用存在不能替代证据支持结论，也不能替代事实审核。

记录逐题输入 → 实际检索 ID → 工具调用/模型轮摘要 → 回答 → 逐维失败原因，按类别和分组汇总；保留延迟 p50/p95。延迟为本地虚构用例耗时，不是服务商性能。无数据的百分位为 `null`，不设置共享 Runner 的脆弱性能门槛。

`real_api_calls=0` 与 `simulated_model_attempts=105` 分开；Token 与费用为 `null`，不套用任何服务商价格，也不把未知费用写成零。以后评测真实模型，须先确定审核资料、协议兼容、授权及服务商实际用量/价格口径，不能重用本命令冒充真实报告。

## 九项可靠性检查

1. 检查点恢复：在临时内存仓储首次提交 CHUNK 时注入失败；再次导入复用 PARSE，第三次复用全部步骤，解析器只调用一次。
2. 模型次数预算：限制两轮后停止，保留已有尝试与稳定错误。
3. 工具次数预算：失败或继续请求也受上限约束。
4. 已报告 Token 预算：报告超过阈值后不再执行后续工具。
5. 未知用量仍受次数上限：四轮模型、三次工具后以 `agent_tool_budget_exceeded` 结束，未知不等于零用量。
6. 上下文字节预算：模拟大 continuation，停止而不暴露其内容。
7. 总用例超时：阻塞的合作式模型协程取消并收敛。
8. 合作式取消：用 Event 确认模型已进入，再取消；没有后续模型调用，保留取消事件，不自动重放。
9. 回答元数据事件：正常搜索/回读/回答产生连续 12 个事件，并有明确终点。

上述检查使用内存替身。数据库检查点恢复和跨进程锁需要独立的 PostgreSQL 验证。异步取消结束本地等待，远端是否继续执行或计费由服务商确认。

## 回答事件接口

`GuideAgentService.answer(request, events=AnswerEventRecorder())` 可选启用事件；不传时旧入口、旧 JSON 和会话存储契约不变。每次问答须使用新的 Recorder，复用在调用模型前拒绝；并发问答各自独立。

事件只有 sequence、kind、index、status、稳定 error，包含回答开始/结束/取消、模型和工具开始/结束。成对顺序检查只允许一个正在执行的操作；模型最多四组、工具最多三组，正常完整最大 16 个事件，Recorder 另设 32 个硬上限。取消后可从外部持有的 Recorder 读取事件；意外异常仍向调用方传播，事件只记稳定 `model_unavailable/agent_unavailable`。

该记录的生命周期限于进程内的单次问答，内容是事件元数据快照。

## 本轮验证与后续

本轮全套离线 `845 passed, 43 deselected in 11.59s`，新增 115 项测试；Ruff 静态检查与 152 个文件格式检查通过，默认/Linux/Win32 strict mypy 各检查 106 个源码并通过，`git diff --check` 通过。实际子进程运行两条 CLI，确认上述退出码和汇总结果；子智能体独立审查与学习文档同步到教学目录。

按用户要求，本轮跳过 PostgreSQL 测试，不重试启动服务；0004/0005 的真实库迁移、跨连接与锁验证仍待补。未读取或修改实际 `.env`，未发付费请求，未删除文件，未提交或推送 Git。

仍待完成：精选真实资料及人工事实/引用标注、正式语义 Embedding、真正独立的保留集、真实模型原生工具协议与质量评测、实际 Token/价格和跨 attempt 费用控制、任务步骤恢复，以及持久化文本流与安全的公开问答接口。当前 45/50 只描述虚构流程检查，不能作为上线质量分数。

## 理解合同回归

对应建议13.14的理解评测部分，复用现有评测CLI：

```powershell
# 默认分别执行规则模式与固定候选回放
uv run python -m teyvatnotes.knowledge.evaluation_cli understanding
uv run python -m teyvatnotes.knowledge.evaluation_cli understanding --mode rules
uv run python -m teyvatnotes.knowledge.evaluation_cli understanding --mode simulated
```

无需`.env`、模型凭据、数据库或网络，不加载真实客户端，不触发检索或答案生成。`--mode`仅适用于`understanding`；原`demo/reliability`命令及已知冲突题失败不变。报告只输出stdout，不自动创建或覆盖报告文件。内容包含原话、确认条件、参考结构与实际结构，仍是私有调试资料，不宜直接公开或进入中央日志。

### 数据集与参考

固定集`understanding-regression-v1`包含31个公开场景、29种不同的“原话＋已确认上下文”。部分场景复用原话，但注入不同坏候选；默认双模式共62次执行，不是62条独立问题，也不是盲测。

覆盖完整/缺条件配队、推荐阵容问法、登记别名、错别字、未知名、明确/模糊指代、删减已确认库存、未知与空库存、任务地点与进度、锁定范围、条件协调、整轮冲突、提示注入，以及模型伪造版本、遗漏否定和重复字段。逗号组合输入在规则模式中拒绝，在人工分句回放中可通过；两种模式分别标注，不要求结果一律相同。

参考候选、补丁、缺失字段、状态和稳定原因由人工显式编写，不调用被测解析器、补丁合并器或缺字段函数反推答案。固定SHA-256涵盖原话、上下文、两种参考、原始候选回放文本和预期尝试数；漂移在读取词表和执行案例前返回`understanding_corpus_drift`，不能静默重新计算并接受。需要修改样本时须同步人工复核参考与固定哈希。报告另记录当前词表哈希、提取提示词哈希和实际耗时；这些元数据并不代替词表内容或语义审核。

`rules`调用实际本地理解用例，不注入模型；`simulated`给同一用例回放人工候选，再经实际解码器与本地校验。这能检验候选能否被安全接纳或拒绝，不能检验服务商会不会生成这种候选、是否支持严格Schema或是否善于理解中文。

### 指标与失败处理

- `contract_pass_rate`：状态、完整候选、完整补丁、缺字段、稳定原因、追问存在性、来源基本一致性及尝试预算全部符合参考的比例。正确追问、拒绝或报错也算通过，不能称作成功回答率或真实理解准确率。
- `check_pass_rates`：上述各维度分别汇总；`accepted_candidate_exact_match_rate`仅统计参考候选非空的场景，`accepted_reference_count`也包含缺条件的部分候选，不仅是`ready`。
- `sources`只检查字段覆盖、原文引文可定位，以及继承值与确认上下文一致；它不证明引文在语义上支持字段值，也不是完整的来源准确率。
- 按模式、类别和实际状态汇总，逐场景保留参考、实际输出、`failed_checks`及剩余调用额度。故意破坏候选、补丁、缺字段、原因、来源或次数的回归测试必须留下失败行。意外案例异常转换为稳定错误并继续记录，其原文不进入报告；取消仍向上传播。
- `real_model_calls=0`，模拟尝试单列；每行同时保留被测用例自报的`reported_extraction_attempts`和替身实际`simulated_extraction_attempts`以核对计数。Token和费用未知保持`null`，不把模拟次数当付费调用。
- `retrieval/answer`明确为`not_run`、错误数为`null`，不是零错误。`real_model_understanding_accuracy/game_fact_correctness`保持`null`，`publication_ready=false`；本命令不能替代真实问答闭环与人工事实验收。

全部符合参考时`status=ok`、退出码0；存在失败行为时`completed_with_findings`、退出码1；语料漂移等执行错误为`error`、退出码1。不自动重试，也不通过删除案例、修改参考或屏蔽失败把报告改成全绿。

### 理解评测验收记录（2026-11-04）

- 全套非数据库回归`1049 passed, 46 deselected in 29.45s`，较此前基线新增38项。Ruff静态检查、169个Python文件格式检查通过；默认/Linux/Win32 strict mypy各检查115个源码并通过，`git diff --check`通过。46项PostgreSQL测试本轮未运行，不沿用历史结果作为本轮通过记录。
- 实际运行CLI的all/rules/simulated均退出0；规则与固定候选回放各31个场景符合参考。实际状态分别为规则：9 ready、17 needs_clarification、4 unsupported、1 error；回放：10 ready、14 needs_clarification、3 unsupported、4 error。通过包含预期拒绝、错误和追问，不是31条都能完成理解或回答。
- 默认all执行62次，固定候选替身调用30次、真实模型调用0；单独rules模拟调用0。旧demo仍为`completed_with_findings`、退出1，reliability为`ok`、退出0；没有掩盖原冲突题失败。
- 实跑`understand "请给薇斯纳推荐一套阵容"`得到角色薇斯纳、目标及关键词配队，状态needs_clarification，追问版本、装备和角色库存，`model_called=false`。支持明确队友替换问法不代表已经验证替代阵容质量。
- 子智能体完成源码与测试静态审查，未发现遗留阻塞项；实际测试由主执行进程完成。未调用真实API、修改实际`.env`、迁移或写入业务数据库、恢复已删除学习文档、删除用户文件或提交Git。真实模型兼容、一般自然语言能力和真实攻略质量仍待单独验证。
