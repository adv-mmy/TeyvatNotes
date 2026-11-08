# 私有攻略会话与玩家条件

独立 CLI 保存已确认的结构化玩家条件，支持追问、替换装备和重新生成建议。自然语言规则、显式模型辅助提取与 JSON 输入复用同一会话用例；不改公开 Task API、Worker、SSE 或旧单轮入口。不引入 Agent 框架，也不宣称具备通用自由文本自动记忆。

## 无网络演示

在项目根目录运行：

```powershell
uv run python -m teyvatnotes.knowledge.conversation_cli demo
```

输出三轮 JSON：装备未知时 `needs_clarification`；补齐示例木剑后 `answer`；明确改成示例木弓后重新检索并返回新答案。最终修订号为 6，历史保留三轮各自的条件、答案和引用。全部是虚构草稿、内存仓储与固定摘录，`model_called=false`，不读取 `.env`、不连接 PostgreSQL、不调用模型服务；不是正式攻略质量证明。

## 正常模式准备

1. 使用业务 `DATABASE_URL`，先备份并核对目标，再由使用者显式执行 `uv run --env-file .env alembic upgrade head`。0005 只新增 `guide_conversations`、索引和约束，不重建任务或知识表。CLI 不自动迁移，不使用 `TEST_DATABASE_URL` 或内存回退。不要通过 downgrade 或删表来解决问题。
2. 导入并人工审核明确版本、非虚构的真实资料，然后显式发布；永久示例不能改标为真实资料投入业务库。
3. 使用原有通用 `LLM_*` 配置，见 [模型配置](model-answering.md)。JSON/规则创建、读取和关闭会话不读取模型凭据；显式 `--extract-model` 会先调用一次提取服务，创建仍不生成答案。答案生成仍等证据就绪才调用。提取和回答都可能计费，开发验证不自动发付费请求。
4. 为本机私有使用者生成并持久保存一个 UUID：

```powershell
python -c "import uuid; print(uuid.uuid4())"
```

将结果填入已忽略的 `.env`：`GUIDE_SESSION_OWNER_ID=<生成的UUID>`。这个值是本地存储分区标识，不是 API Key、密码、身份认证或公开多租户安全机制。更改后无法通过本入口读取原标识的会话；恢复原值即可读取。不要自动为每次命令生成新值。

## 命令与条件更新

初始 JSON 的 `question` 必填，其余字段沿用 [问题准备](knowledge-retrieval.md) 的定义。可以缺条件，以便下一轮返回追问。示例字段需换成真实已审核范围：

```json
{
  "question": "这个角色怎样培养？",
  "guide_kind": "character",
  "game_version": "已审核版本",
  "entity": "已审核角色",
  "keywords": ["装备"],
  "goal": "装备",
  "owned_equipment": null,
  "owned_characters": ["已审核角色"]
}
```

```powershell
uv run --env-file .env python -m teyvatnotes.knowledge.conversation_cli create initial.json
# 保存输出的 conversation_id；创建只存初始条件，不生成答案，revision=0。
uv run --env-file .env python -m teyvatnotes.knowledge.conversation_cli show <conversation_id>
uv run --env-file .env python -m teyvatnotes.knowledge.conversation_cli ask <conversation_id> followup.json --revision 0
uv run --env-file .env python -m teyvatnotes.knowledge.conversation_cli show <conversation_id> --history
```

`followup.json` 的 `question` 每轮必填。首次可以只有问题；收到追问后通过下一份 JSON 明确补充缺项。更换装备示例：

```json
{
  "question": "请按我现在拥有的装备重新给出建议。",
  "owned_equipment": ["已确认装备"],
  "conditions": {"装备": "已确认装备"}
}
```

| 输入方式 | 合并行为 |
| --- | --- |
| 省略字段 | 继承当前已确认值 |
| 提供 `owned_equipment` / `owned_characters` 列表 | 整项替换，非追加 |
| 装备或角色为 `null` | 未知，需要按原问题准备规则追问 |
| 装备或角色为 `[]` | 已明确没有，不能当作未知 |
| 提供 `conditions` 映射 | 整项替换；`{}` 清空，不逐键合并，`null` 非法 |
| 更改已确认的版本、实体、攻略类型 | `conversation_scope_change`，必须新建会话 |

最初未知的版本、实体或类型可以补全，确认后不能改为其他值或清空。装备列表与任意 `conditions` 键不会自动做语义协调：修改装备时，调用者也应明确更新相关适用条件；系统不会从“换一下装备”自动猜出新列表。

每次 `ask` 使用 `show` 当前返回的修订号，不猜测、不自动重试。接受新轮时修订号 +1，成功记录答案或错误时再 +1，因此正常三轮为 0→2→4→6；关闭再 +1。竞争失败返回 `conversation_conflict`；已有 pending 轮返回 `conversation_busy`。已过期修订在提取前预检拒绝；若提取期间发生竞争，可能已消耗一次提取，最终 CAS 仍拒绝覆盖，不生成答案、不重放。

## 自然语言规则入口

仅查看理解结果（不读 `.env`、不连接数据库或模型，不保存）：

```powershell
uv run python -m teyvatnotes.knowledge.conversation_cli understand "薇斯纳应该怎么配队？"
# 可选：从已确认的结构化条件继承，不从历史答案猜测库存。
uv run python -m teyvatnotes.knowledge.conversation_cli understand "她如何配队？" --context samples/questions/team.json
```

第一条会识别角色、配队目标和检索词，并追问版本、角色列表与装备列表。结果有 `ready / needs_clarification / unsupported / error`、候选请求、受校验补丁、缺失字段、歧义、追问及字段来源。理解元数据不混入原有问题 Schema；`ready` 只说明输入齐全，未保证有可用攻略。

持久化多轮（需要前述业务数据库配置；条件齐全的 `ask` 可能产生模型费用）：

```powershell
uv run --env-file .env python -m teyvatnotes.knowledge.conversation_cli create --text "薇斯纳应该怎么配队？"
# 用真实返回的ID和当前revision；create仍然只保存条件，不回答。
uv run --env-file .env python -m teyvatnotes.knowledge.conversation_cli ask <ID> --revision 0 --text "版本：7.1"
# 示例库存必须改成玩家实际已确认持有的名单，不是默认拥有全部角色和装备。
uv run --env-file .env python -m teyvatnotes.knowledge.conversation_cli ask <ID> --revision 2 --text "角色列表：薇斯纳、奥黛塔、砂糖；装备列表：蝶变"
uv run --env-file .env python -m teyvatnotes.knowledge.conversation_cli ask <ID> --revision 4 --text "我没有奥黛塔"
```

版本必须明示，例如 `版本：7.1`，不推断“最新”。支持 `名字应该怎么配队 / 名字如何组队 / 名字怎么培养 / 名字怎么配装`，以及 `给名字推荐配队 / 请给名字推荐一套阵容 / 名字怎么搭配阵容`，目标分别映射到受控关键词。分句用分号、句号或换行；列表用顿号或逗号。任务输入支持 `任务：名称；版本：7.1；地点：地点；进度：步骤；关键词：查询词`，不假定词表包含完整任务名录。

- 已登记别名（例如奥萝塔→奥黛塔）可以更正；错别字只给候选并追问，不自动选择。
- `角色列表：未知` 表示 `null`；`角色列表：无` 表示 `[]`。装备同理。没有提到的字段继承已确认值。
- `我没有奥黛塔` 及其后接 `，能换成谁 / ，可以换成谁 / ，有什么替代`，只在此前已确认完整角色列表时生成完整替换列表；库存未知时追问，不生成空列表。不会增加推荐队友到持有列表，也不会在理解阶段直接生成替代阵容。未覆盖的后缀不被静默忽略。
- `她` 仅能指当前唯一已确认角色；`这个队友` 不从旧答案猜测，必须给出名字。
- 未理解分句、冲突值、未知名称、改变锁定范围时整轮不保存。已有非空 `conditions` 时不自动协调库存变动，应使用 JSON 同时明确条件和库存。
- 明确但缺条件的补充可保存为追问轮；会话 module 根据本次理解结果进入既有 `ask` 追问分支，直接记录追问，绝不进入检索或答案生成。歧义不领取轮次；保存的追问消耗一个会话轮次，正常修订 +2。
- 原文最多 1000 字符、4096 UTF-8 字节、20 个分句；继续复用原字段、库存和会话预算。自然语言 `ask` 在理解前检查修订/关闭/忙碌状态，写入仍经 CAS，不自动重放。

默认规则模式没有理解模型调用：提取尝试为 0，未知 Token/费用为 `null`，不把答案模型用量归零。理解用例为异步 `await service.understand(text, confirmed_context)`，原同步入口已原位替换，不提供兼容包装。

自然语言 `create/ask` 的预检、理解、追问短路和 CAS 领取统一由现有 `ConversationService` 编排；理解器通过延迟工厂在预检通过后创建。独立 `understand` 仍直接调用理解用例，不要求会话或数据库。CLI 负责配置、输入输出和连接清理，命令与 JSON 输出格式不变。

固定理解回归可运行 `uv run python -m teyvatnotes.knowledge.evaluation_cli understanding`，不读取模型配置或连接数据库。它比较规则结果与人工固定候选回放，不测试真实模型能力；评分口径见 [理解评测](evaluation.md#理解合同回归)。

### 显式模型辅助提取

添加 `--extract-model` 才会调用当前 `LLM_*` 配置的模型；没有厂商/模型默认值，不改 `.env`，不自动尝试其他模型，不在失败后回退到规则模式。下列命令都可能产生提取费用：

```powershell
# 单独检查提取，不保存，不连接数据库；即使缺条件，也可能已花费一次提取。
uv run --env-file .env python -m teyvatnotes.knowledge.conversation_cli understand "请问薇斯纳应该怎么配队，当前版本是7.1" --extract-model
# 创建会话只提取并保存通过校验的明确条件，不生成答案。
uv run --env-file .env python -m teyvatnotes.knowledge.conversation_cli create --text "薇斯纳应该怎么配队？" --extract-model
# 明确使用当前修订号；条件齐全时继续进入已有检索/工具回答。
uv run --env-file .env python -m teyvatnotes.knowledge.conversation_cli ask <ID> --revision <当前修订号> --text "她如何配队？" --extract-model
```

`--extract-model` 不能用于 JSON 文件输入。普通 `understand` 仍可完全离线；加载 `--context` 是读取调用者提供的已确认条件文件，不是自动回放历史。

模型只负责提出分句和候选字段。线协议为必填 `status` 与 `segments`；每段包含 `start/end/quote/updates`，其中 `updates` 是闭合 `{field,value}` 列表。仅允许九种可提取字段，值只能为字符串、字符串列表或 `null`；`question/conditions`、重复字段、额外属性、数字/布尔值、重复 JSON 键、NaN 和超 16 KiB UTF-8 响应均拒绝。不保留旧动态字典格式。远端 `text.format` 沿用通用配置，但第三方 strict 兼容仍未经真实验证，不能因为本地 Schema 通过就声称服务商支持。

片段必须逐字对应本轮原话，按顺序、不重叠，覆盖所有非分隔内容。每段候选经词表规范化后，必须与同一本地规则校验器生成的完整字段集合和值一致；之后再次聚合检查整轮冲突、库存及锁定范围。不能裁掉否定字、把候选改写成肯定句、从旧答案填版本/库存，或把局部删减当空库存。未知名称或不可校验表达不会被模型“高信心”放行。

因此这是**模型辅助分句与候选提取**，仍受有限可验证语法约束，不是一般口语理解。模型可以帮助拆分“问题，版本，库存”等组合输入；自由改写、复杂指代、任意条件和未覆盖问法仍可能被拒绝或要求明确输入。仅当前原话与已确认结构发送给提取模型，旧 `question` 和历史答案不发送。提取不创建工具权限、不联网抓攻略、不执行原话中的命令。

### 联合预算与实际消耗

- 同一显式模型命令建立一个 `GenerationLedger`，提取与工具回答共用最多4次模型尝试、90秒生成窗口和20000已报告Token软阈值。提取只调用一次、最长30秒且受剩余总时限约束；成功后最多剩3次回答尝试。若最后一次模型调用提出工具请求，会在执行工具前停止，因为已没有机会回送结果。
- 完整提取上下文（system、user、Schema及协议封套预留）UTF-8预算48k，在读取凭据前检查；不静默裁剪已确认条件。模型返回后还会再次检查总截止时间和已报告Token。Token是事后报告阈值，不是硬费用上限；未知Token及费用仍是 `null`。
- 输出 `round_usage` 同时给出整轮、`understanding` 和 `answer` 的尝试、Token、模型及各阶段模型调用耗时；理解结果本身的 `elapsed_ms` 另含本地验证。`ask` 历史回复顶层 `generation_attempts/model_called/input_tokens/output_tokens/elapsed_ms` 表示整轮，不再把答案阶段的零调用当作整轮零调用。缺条件追问可能是提取1次、答案0次。
- 陈旧修订、已关闭、busy或轮次满会话在提取前预检，零提取。提取期间出现竞争、提取坏输出、存储或清理失败仍报告已经发生的消耗，不自动重试。若命令报错但存储可能已完成，先 `show` 核对，不盲目再次执行付费命令。
- 提取超时/非法输出不进入检索或答案生成。异步取消继续抛出 `CancelledError`（`UnderstandingCancelled.result` 保留提取统计），不转为成功；远端可能已计费。共享截止不是数据库SQL或CLI全进程的强制终止器，也不是服务商停止计算的保证。
- `understand/create` 提取统计只在当次输出，不作为持久费用账本；`ask` 已落库回复保留当时的整轮统计。失败前尚未领取轮次的提取不会伪造一条已保存历史。没有公开聊天API、认证或全局跨进程费用控制。

内部调用通过每次独立的 `ConversationSubmission` 向 `create/ask` 传递预算并读取本次理解结果；理解拒绝时返回 `None`，不会创建或领取会话轮次。未显式提供预算但发生模型提取时，会话 module 自动保留同一账本供后续回答使用；规则零提取不新增整轮统计。回答 adapter 的 `answer(..., generation=...)` 显式接收同一账本，构造器不再保存请求预算。理解结果在写库前写入提交记录，外层仍持有账本，因此 CAS、保存或连接清理失败后可以如实报告已发生的消耗；该提交记录不写入会话历史，也不跨请求共享。

## 历史、并发与失联恢复

每轮保存当时的完整条件快照和受控答案；新轮 pending 时 `latest_reply=null`，不能把旧答案展示为当前建议。`--history` 才显示私有完整历史。每轮重新创建问题准备、工具会话和引用账本，只把当前明确条件交给模型，不向提示词回放旧答案、原始工具参数或服务商 opaque/reasoning 数据。

PostgreSQL 通过所有者与会话 ID 查询，并在短事务内 `SELECT ... FOR UPDATE` 校验修订再提交。领取、完成和关闭分别原子更新，数据库事务与行锁不跨模型网络请求。历史前缀、身份和既有范围不能由仓储更新顺带改写。取消合作传播；正常可记录 `cancelled`，若取消期间存储不可用则可能留下 pending。

进程退出或失联后，先读取现状，人工核实，再显式标记中断：

```powershell
uv run --env-file .env python -m teyvatnotes.knowledge.conversation_cli show <conversation_id>
uv run --env-file .env python -m teyvatnotes.knowledge.conversation_cli interrupt <conversation_id> --revision <当前修订号>
# 再读最新修订号，自行决定是否开启下一轮；不存在自动重放。
uv run --env-file .env python -m teyvatnotes.knowledge.conversation_cli close <conversation_id> --revision <当前修订号>
```

中断后的迟到回复不能覆盖新轮；中断只是本地状态记录，不保证原服务商计算或收费停止。用户明确重新提问也可能再次付费，不承诺 exactly-once 计费。关闭须无 pending 轮，关闭后不能继续提问或重开，不删除历史。

## 预算与隐私边界

- 每会话最多 20 个已领取轮次，包含澄清、错误、取消和中断；超限仍可读取或关闭，没有无限历史。
- 输入 JSON 最多 16 KiB，拒绝重复键、非法 UTF-8、NaN 与未知字段；受控回复最多 65,536 UTF-8 字节，整条存储历史最多 2,000,000 字节。
- 会话应用生成等待默认 100 秒；内部工具用例继续受 90 秒、4 模型/3 工具尝试等原限制约束。这不是数据库操作或 CLI 全进程的硬截止，也不是 Token/费用硬上限。
- 未配置真正的语义 Embedding，默认仍使用既有关键词检索；不把保存会话描述为解决了真实语义召回、事实正确性或所有装备前提。
- 私有 JSONB 明文含玩家问题、装备/角色条件、答案、引用及位置链接。没有加密、自动保留期、删除 API 或公开认证接口。所有者谓词只是可信本机 CLI 的分区规则；持有同一数据库账号的人仍可能直接读取其他行。
- CLI 成功输出属于主动查看的私有数据，不能直接写入公开 API、中央日志或分享给其他人。错误只返回稳定代码，不输出连接串、凭据或底层异常。
- 引用/装备结构检查沿用旧回答规则，不等于自然语言语义支持验证。本轮不实现 50 题真实答案评测、流式文本、跨 attempt 费用控制或任务步骤恢复。

## 验证范围

新测试覆盖补充/替换条件、范围锁定、深拷贝、异常和字节预算、修订竞争、取消/超时、人工中断、迟到回复、不可改写历史、跨连接持久化、迁移保留旧数据和私有 CLI。数据库验证仅使用专用可丢弃数据库及随机 schema，业务库没有自动升级；真实模型、正式攻略质量和实际费用不在此次离线/数据库验收范围内。

### 本轮验收记录（2026-10-04）

- 最终全套离线 `730 passed, 43 deselected in 9.61s`；新增 65 项离线测试。三轮演示实跑得到修订号 2/4/6 与澄清/木剑/木弓结果，均 `model_called=false`。
- Ruff 全树静态检查及 144 个 Python 文件格式检查通过；默认、Linux、Win32 strict mypy 各检查 101 个源码并通过，`git diff --check` 通过。
- 新增 8 项 PostgreSQL 测试已编写，但首次连接因本机服务停止而拒绝；用户随后明确要求本轮忽略数据库测试，未再重试。43 项数据库测试均未计入本轮通过基线。0005 离线 DDL 生成/元数据检查通过，不代表真实数据库迁移、跨进程锁和持久化已验收。
- 未自动迁移业务库，未变更实际 `.env` 或原模型配置，未发付费请求、未删除文件、未提交或推送 Git。学习文档由子智能体同步到教学目录。

### 自然语言规则原型验收（2026-11-03）

- 固定理解回归覆盖标准名与别名、未知名候选、缺版本与库存、空列表/未知/继承、指代、整项删减、冲突分句、范围锁定、条件协调和非法输入；CLI注入测试覆盖创建、追问保存、条件齐全调用原回答器、歧义不写入和旧修订在解析前拒绝。
- 非数据库全套 `933 passed, 46 deselected`；本轮新增37项。Ruff、161个Python文件格式和111个源码strict mypy通过。46项PostgreSQL测试本轮未运行，不沿用历史结果冒充本轮通过。
- 复用已存在的米游社78297730薇斯纳名称校正摘要，在临时内存仓库中显式发布，完成问题→追问→版本补充→明确库存→带引用摘录→指定删队友。修订0/2/4/6，后两轮各2条摘录、2个引用、3次固定模拟器尝试，库存3→2。
- 上述回答为固定摘录模拟器，不是自然语言模型理解、真实模型回答或替代阵容质量验收。没有真实模型调用、数据库写入、业务迁移、密钥修改、文件删除或Git提交。

### 模型辅助提取与联合预算验收（2026-11-03）

- 本轮新增78项离线回归；冻结代码全套 `1011 passed, 46 deselected in 25.68s`。Ruff通过，166个Python文件格式检查通过；默认/Linux/Win32 strict mypy各检查113个源码并通过，`git diff --check`通过。46项真实PostgreSQL测试本轮未运行，不能用历史PG结果代替新路径验证。
- 固定人工候选与FakeModel覆盖闭合Schema、UTF-8预算、重复键/字段、错误类型、片段乱序/重叠/遗漏否定、伪造条件、别名、空库存/未知、整体冲突、任务字段、取消、超时及失败统计。CLI注入测试覆盖提取期间修订竞争、陈旧/关闭/busy/轮次满预检、坏输出不回退、清理失败保留用量、取消不被清理异常掩盖。
- 共享账本回归验证提取1次后回答只剩3次、末次调用不能再执行工具、已报告Token软阈值及不重启的总截止时间。测试替身的调用次数是模拟尝试，不是付费请求或真实服务商Token验证。
- 复用原有米游社78297730名称校正摘要，在临时内存显式发布，固定提取候选→追问→补版本→登记alias库存→带引用摘录→指定缺少奥黛塔。修订0/2/4/6；缺条件的已保存追问轮为模拟提取1/回答0，后两轮各模拟提取1/回答3/合计4，取得2条摘录和2个引用，库存3→2。
- 上述真实来源身份的有限摘要与固定候选/摘录模拟器，不证明真实模型提取兼容、一般口语理解、语义引用支持或替代阵容质量。本轮真实模型调用0，未写业务数据库、迁移、修改实际`.env`、恢复已删除学习文档、删除用户文件、提交或推送Git。独立子智能体仅作源码/测试静态审阅，未发现遗留阻塞问题；实际测试由主执行进程完成。
