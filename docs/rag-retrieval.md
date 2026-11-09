# RAG 检索底座与离线对比

本轮对应学习建议 13.11 第 4 项的基础设施与离线验收。回答仍用用户已指定的 OpenASI `gpt-5.6-sol`，未改变 `.env`、回答配置或旧 CLI。Responses 文本输出和 Embedding 数字数组是不同数据契约；当前没有确认该第三方模型的 Embeddings 可用性，因此不探测付费接口、不擅自选模型，也不把文字转换成“真实语义向量”。

独立向量接口参考了 [OpenAI 官方 Embedding 数据契约](https://developers.openai.com/api/docs/guides/embeddings)。这只是接口设计依据，不是 OpenASI 对模型可用性、价格或兼容性的承诺。

## 已实现与尚未完成

| 内容 | 本轮边界 |
| --- | --- |
| 分块与证据 | 复用原有正文块/章节切块、稳定证据 ID、字幕时间及文章位置；角色相邻块、任务前序块回读仍分开处理。没有升级切块算法或覆盖旧版本。 |
| 元数据过滤 | 正式只读 `published` 非 fixture；demo 只读 fixture 草稿。精确版本/类型下先按来源选择最新可见版本，再过滤实体和条件。 |
| 向量接口 | 供应商无关 `EmbeddingClient`；模型/修订/维度/演示性质组成索引身份；原文和章节哈希绑定向量。 |
| 索引与持久化 | 复用已有正确向量，缺失部分分批生成，全部成功后一次原子保存；内存和 PostgreSQL JSONB 实现。 |
| 检索 | `keyword`、`vector`、`hybrid`；混合采用倒数排名融合，不直接加字面分与余弦值。 |
| 离线演示 | `DemoLexicalEmbedding` 使用字符 n-gram + SHA-256 特征哈希；无网络、无训练权重，只准 fixture。 |
| 固定对比 | 12 题 × 3 模式，人工标注相关块 ordinal，报告 Recall@k、MRR、负例命中、版本/条件越界和上下文缺口。 |
| 正式语义 Embedding | **未完成**：尚无用户指定且验证可用的文本向量模型或客户端。Sol 回答配置不变。 |
| 重排序与正式质量 | 未接交叉编码器；尚无真实审核资料、同义表达质量基准或约 50 题完整答案评测，不根据虚构结果断言语义提升。 |

## 无费用演示

在 `D:1code1TeyvatNotes` 中执行：

```powershell
uv run python -m teyvatnotes.knowledge.rag_cli compare --demo
uv run python -m teyvatnotes.knowledge.rag_cli search --demo --mode keyword --text "第二步示例机关" --keyword "机关" --version DEMO-ONLY --kind quest --entity "示例任务甲" --condition "前置任务=已完成示例前置"
uv run python -m teyvatnotes.knowledge.rag_cli search --demo --mode vector --text "第二步示例机关" --version DEMO-ONLY --kind quest --entity "示例任务甲" --condition "前置任务=已完成示例前置"
uv run python -m teyvatnotes.knowledge.rag_cli search --demo --mode hybrid --text "第二步示例机关" --keyword "机关" --version DEMO-ONLY --kind quest --entity "示例任务甲" --condition "前置任务=已完成示例前置"
```

demo 每次在新内存库载入两份长期虚构快照并建立索引，不持久到业务库。`answer_model_called=false`、`paid_embedding_called=false`、`semantic_embedding=false`，费用 `null`。关键词模式不调用向量端口；demo 入口仍预建索引用于同库对比。返回证据正文、库内派生引用、两路原始分/排名和融合分、补回上下文及 `context_truncated`，没有生成的攻略答案。

命中任务第二步时从前置块开始回读，`--max-chunks 1` 可看到截断标志。未截断仅代表按本仓储范围返回的前序/相邻块连续，不保证资料本身完整、包含所有步骤或支持最终结论。章节/正文包含字面重叠会使哈希向量相似；这不代表理解“伤害低”等表达，更不证明游戏事实正确。

## 存储、模型身份与迁移

`guide_chunk_vectors` 用 `(profile_id, chunk_id)` 唯一标识向量，外键回到不可变原文块；保存配置、维度、正文哈希与数字数组。对相同键重复保存同样数据幂等，不同数据报 `vector_conflict`，不覆盖历史索引。修改模型、维度、预处理或切块输出必须产生新身份，不能混用旧向量。

升级路径为 `20261003_0003 → 20261004_0004`。本轮不自动迁移业务库；用户需要持久化时，先核对 `DATABASE_URL` 指向正确开发库，再执行：

```powershell
uv run --env-file .env alembic upgrade head
```

仅新增表，不要求 pgvector 扩展、不升级依赖、不重建既有任务/资料表。不要执行 downgrade、删除 schema/表/卷来“修复”。JSONB 存向量是小规模可运行方案，不是 ANN 或生产级向量数据库：候选目录最多 500 块，超过预算明确失败，不悄悄取前缀或退回旧版。

正式关键词检索可以使用已配置的业务 PostgreSQL：

```powershell
uv run --env-file .env python -m teyvatnotes.knowledge.rag_cli search --mode keyword --text "自己的问题" --keyword "准确术语" --version "已审核版本" --kind character --entity "已审核实体" --condition "已确认条件=对应值"
```

新 CLI 正式 `vector/hybrid` 目前在连接数据库前返回 `embedding_not_configured`，不调用 Sol Embedding 或其他付费接口。程序端可显式注入符合协议的客户端与 `RagIndexService`，然后注入 `RagSearchService`；必须同时确认模型真实可用、输出维度、成本与隐私规则，不能只更改 profile 字段就认为语义能力已验证。

## 排序与版本约束

字面规则沿用正文命中 +4、章节命中 +2，同词去重；向量用余弦，相似度大于 0 才参加向量召回。混合把两路全候选有序排名合并，对每路命中加 `1 / (60 + rank)`，再按文档 ID、ordinal、证据 ID 稳定打破同分。排名、原始分和融合分均不是答案置信度。没有真实评测前不把大于 0 称为可靠相关性阈值。

所有排序都在仓储筛选之后。新版本无索引时报 `rag_index_incomplete`，不召回过时有索引版本；正文哈希、数量、profile、维度不匹配也不悄然降级。调用方可以主动另发 `keyword` 查询，但结果必须明确标记模式。

`RagKnowledgeReader` 是每问题独立创建的显式适配器，可以注入旧问答/工具服务；只授权本实例搜索实际返回的 ID，同 scope 回读并核对原文身份。旧入口不会自动使用它；旧问答准备仍要求显式关键词，缺玩家条件时先追问。新 `rag_cli search --mode vector` 支持无关键词检索文本，但目前只在 demo 或程序注入端口上运行，不应写成正式问答已支持无关键词语义理解。

## 预算、取消与恢复

- 单份索引默认最多 500 块、章节+正文合计 128,000 字符，每批默认 16；维度最多 4096。这些是字符/条数上限，不是模型 Token 或价格承诺。
- 索引/检索/单次回读默认各 30 秒。它们是独立应用用例时限，不是 CLI 全进程硬截止；多次索引、查询和回读的总耗时可能更长。
- 全批成功后一次保存。批内错误或取消时保留原有向量，重启后复用已持久化的条目。
- CPU 特征与大候选余弦计算移入有界线程工作；取消传播但不强杀线程，也不证明远端服务停止计费。没有自动重试、自动回退或在线请求。
- 真实客户端失败可能已产生费用。未知用量和单价以 `null` 表示。
- 错误只输出稳定代码，不打印数据库 URL、凭据、正文或原始异常。查询成功输出是主动调用的私有教学 CLI，不应直接作为公共 API/集中日志。

## 评测口径与下一步

`compare` 的 12 题包括 8 个有参考块的正例（其中一题故意给不命中的关键词）和 4 个版本/实体/条件负例。每种模式在正例上算宏平均 Recall@k 与 MRR；负例单独算命中数量。结果同时保留每题命中 ID、版本、排序分、上下文 ordinal、截断和耗时，用于定位失败。

本轮 `k=3` 实际离线结果：词面宏平均 Recall/MRR 为 `0.875/0.875`，词面向量与混合均为 `1.0/1.0`；三种模式负例命中、范围越界和上下文缺口均为 0。这是 7 块虚构文本上特意包含“词面无命中”题的流程检查，不是正式精度、自然语言泛化或真实语义优越性证明。

参考标注找不到会报 `retrieval_fixture_drift`，不会把题悄悄改成负例。候选范围、邻接连续性可以自动检查；自然语言结论的语义支持、装备真实可用性和游戏步骤正确性不在这份离线检索对比中。

正式语义检索仍需确认一个真正返回数字向量的客户端（若仍用现有平台/模型，先由平台确认对应能力），在人工审核的角色与任务资料上复用这些接口，再比较自然表达/术语召回、必要的重排序和真实答案引用支持度。RAG 本轮没有实现会话；随后按用户继续开发的要求新增了独立 [私有会话](conversations.md)，它不改变默认检索，也不代表语义 Embedding 的待办已完成。

## 本轮验收记录（2026-10-04）

- 最终全套 `668 passed in 19.23s`，无 skip，包含 35 项真实 PostgreSQL；新增 RAG 129 项，其中 121 项离线、8 项真实随机 schema 验证。
- Ruff 全树检查通过，133 个 Python 文件格式检查通过；默认、Linux、Win32 的 strict mypy 各检查 95 个源码文件并通过，`git diff --check` 通过。
- 数据库测试仅使用任务专用可重建测试库；新知识测试 schema 与临时测试证据保留。业务库没有自动迁移，未修改模型配置、未发付费请求、未删除文件、未执行 Git 提交。
- 新 RAG 数据库测试只创建随机 schema 并增量升级；全套旧 PostgreSQL 测试含回滚到 base 后重建，因此只能指向上述专用可丢弃库，不能直接加载业务库配置运行。
- Code Review 与逐函数 Python 语法讲解由子智能体维护在教学目录，不在产品仓库创建学习文档。
