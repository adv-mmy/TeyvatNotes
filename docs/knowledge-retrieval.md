# 关键词检索、证据回读与问答准备

日期：2026-10-03。本页描述第二阶段的无模型前置入口 `knowledge/query_cli.py`，对应建议文档第 13.8、13.9 节和第 13.11 节第 2 项的检索/引用部分。后续已新增独立的 OpenASI 接入与单轮回答 CLI，见 [模型与回答说明](model-answering.md)；旧准备入口仍不读取凭据或生成答案。模型实现通过离线测试，真实调用与真实攻略质量尚待验证。

## 1. 当前可运行功能

2026-10-04 更新：体验服与正式服采用同一检索/回读和 RAG 范围规则；`content_nature` 作为来源元数据保留并进入模型证据与引用展示，不按该字段排除资料。正常范围仍是已发布、非虚构、完整且版本匹配的资料；demo 仍仅虚构草稿。旧数据库需要显式应用 `0006`，此轮真实数据库验证暂缓。

- 显式关键词字面检索，按准确游戏版本、攻略类型、实体和玩家已确认条件过滤。
- 以数据库已存在的文本块 ID 回读上下文，输出来源、文档版本、段落/图片位置或视频分 P 和时间链接。
- 问答准备先检查必要字段，不足时追问；无证据或上下文截断时说明缺口。
- PostgreSQL 查询为只读，不自动迁移、不调用模型、不执行网络抓取、不写玩家会话。

这里没有自然语言实体识别、俗称解析、分词、Embedding、向量检索、Agent 工具循环、模型答案或回答流。`search_knowledge/read_evidence` 是只读应用端口，不代表模型已经能够选择调用它们。第一阶段任务类型、Worker 和 HTTP/SSE 契约不变。

## 2. 先运行完全离线的演示

```powershell
cd D:1code1TeyvatNotes
uv run python -m scripts.guide_retrieval_demo
uv run python -m teyvatnotes.knowledge.query_cli prepare-question samples/questions/quest.json --demo
uv run python -m teyvatnotes.knowledge.query_cli prepare-question samples/questions/clarification.json --demo
uv run python -m teyvatnotes.knowledge.query_cli search --keyword 装备 --version DEMO-ONLY --kind character --entity 示例角色甲 --condition 装备=示例木剑 --demo
```

演示结果分别展示追问、角色装备证据和任务前序证据。返回 JSON，不启动聊天页面、不生成新文件。`--demo` 每次重新加载两份固定虚构样本到临时内存，因此其稳定证据 ID 可在下一次演示命令中重建；这不意味着内存数据持久化。

知识 CLI 和两种教学演示的 stdout 统一为 UTF-8 JSON，Windows 管道/重定向的读取程序同样应使用 UTF-8，不依赖本地 GBK 代码页。常规交互终端仍直接显示中文。

示例版本 `DEMO-ONLY`、角色、装备、任务和地点均为纯虚构，不是真实原神攻略。演示仅允许虚构 `draft`，不会发布资料、不读取数据库或模型凭据。正常模式则只允许非虚构 `published`；没有“包含任意草稿”的开关。

## 3. 使用实际 PostgreSQL 资料

先准备经过人工审核、版本明确的快照，按 `guide-ingestion.md` 显式迁移并导入发布。业务连接来自 `GUIDE_DATABASE_URL`；不得把 `TEST_DATABASE_URL` 当正常数据源。

```powershell
# 仅当本地 .env 已配置业务 GUIDE_DATABASE_URL 时使用 --env-file。
uv run --env-file .env python -m teyvatnotes.knowledge.query_cli search --keyword "待查关键词" --version "准确版本" --kind quest --entity "资料中的任务名" --condition "前置任务=已完成"
# 替换为 search 返回的 evidence_id；读证据时同样提供查询范围。
uv run --env-file .env python -m teyvatnotes.knowledge.query_cli read-evidence <evidence_id> --version "准确版本" --kind quest --entity "资料中的任务名" --condition "前置任务=已完成"
uv run --env-file .env python -m teyvatnotes.knowledge.query_cli prepare-question <question.json>
```

`read-evidence` 不接受用户提供的来源 URL；引用由程序从数据库已有来源与定位信息生成。不存在、错误版本、错误实体或条件不满足的 ID 一律返回 `evidence_not_found`，不透露被排除资料的内容。

## 4. 确定性检索规则与边界

1. 版本和实体**精确匹配**，不推断“当前版本”或将未知版本混入。
2. 对同一来源、版本和攻略类型，先选择 `(created_at, id)` 最大的可见文档，再做实体/条件/关键词筛选。新的草稿不会隐藏旧发布版；新的发布版不匹配关键词或条件时，也不回退到旧版。
3. 资料声明的**全部** `conditions` 必须包含于玩家已确认条件且值一致。玩家额外条件允许存在，缺少任何资料前提都不视为满足。不做范围/数值/语义推理；`命座=2` 不自动满足 `命座=1`。
4. 关键词用 `lower()` 去重后按字面子串匹配：每词正文命中 +4，章节命中 +2。不按出现次数放大，不仅凭标题召回；分数不是概率或答案置信度。
5. SQL 使用参数化 `strpos(lower(column), term)`，`%`、`_`、引号等均是普通字符，不是通配符或 SQL。内存按同一规则扫描。当前未做 Unicode NFKC/俗称归一或多语言搜索专项验收。
6. 顺序为分数降序、文档 ID 升序、块序号升序；单次最多 12 个关键词、10 个命中。 PostgreSQL 在数据库计算并限制返回结果，没有把全库加载到应用中。当前未建立 FTS/向量/专用关键词索引，规模化性能尚未验收。

同一明确版本的不同来源可能给出冲突建议；当前同时返回证据及类别，不自动判断哪项事实正确，也不跨来源去重或总结。

## 5. 回读范围与引用

- 角色资料：命中块及左右各一块。
- 任务资料：从文档第一个文本块到命中块后一块，按原序返回，用于保留前序文字和前置条件。
- 每次最多 20 个上下文块（可减小）；超过预算设 `context_truncated=true`，问答准备降级为证据不足，不能宣称步骤完整。
- 精确历史 `evidence_id` 仍可按同版本/实体/条件回读；自动检索只选最新发布版，已有历史引用不因此失效。

**这是有界已存文本上下文，不保证完整父段落、完整攻略或所有相关资料。** 第一周长段落可能被字符切块，左右一块未必覆盖完整父段落；任务命中后的完整后续步骤也未必返回。定位保留字符偏移，原快照与解析产物仍在原仓储中，完整父段落读取和类型化切块属于后续 RAG 完整化。

B 站引用链接包含保存的 `p` 与起始秒 `t`，并同时返回原起止时间和字符偏移。米游社使用真实保存的文章链接和段落/图片位置，不发明无法验证的页面锚点。来源存在不代表引用支持某个尚未生成的结论。

## 6. 问题 JSON 与三个结果

输入最大 16 KiB，UTF-8（允许 BOM），拒绝未知字段、空白字段和隐式数字转字符串。参考 `samples/questions/`。

```json
{
  "question": "示例任务甲第二步有哪些证据？",
  "guide_kind": "quest",
  "game_version": "DEMO-ONLY",
  "entity": "示例任务甲",
  "keywords": ["第二步"],
  "conditions": {"前置任务": "已完成示例前置"},
  "location": "虚构地点",
  "progress": "第一步已完成"
}
```

共有字段为 question、guide_kind、game_version、entity、keywords、conditions。角色还需 goal；任务还需 location 和 progress。后两项目前只作为已明确字段检查，**不自动变成条件过滤**；已满足的资料前提需显式写在 conditions。自由文本 question 当前用于前置输入，不自动抽取关键词或核对结构化字段与文字是否一致；不能把这一版当聊天理解器。

| status | 意义 |
| --- | --- |
| needs_clarification | 必要字段不足，返回追问，应用用例不调用知识仓储。 |
| insufficient_evidence | 当前范围没有匹配证据，或有序上下文超出预算。 |
| evidence_ready | 返回库内证据和位置，仅供之后模型处理，不是攻略答案。 |

输出始终 `model_called=false`，没有 `answer` 字段；示例附 `demo_only=true` 和 `fictional_demo_only`。CLI 非演示模式要求配置数据库，即使请求最终只需要追问也不会静默替换后端。业务三种结果退出码均为 0，输入/配置/存储错误为 1。错误只输出稳定代码，不输出路径、玩家原文、凭据、数据库 URL 或异常堆栈。

## 7. 后续仍待完成

模型服务商与 API 由用户稍后提供；届时实现实际模型适配、结构化答案及引用/玩家条件校验、超时/用量/费用与真实问答验收。真实来源选择/抓取验证、类型化切块、语义或混合检索、工具选择循环、会话条件和攻略专用 Runtime/事件仍按建议文档的阶段顺序推进。不得把当前无模型示例算作这些部分已完成。
