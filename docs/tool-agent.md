# 最小只读工具调用循环

对应学习建议 13.11 第 3 项。本轮手写工具循环，不引入 Agent 框架、MCP、Embedding、会话或公开攻略 HTTP。旧单轮 `answer_cli` 和 Task API/Worker 不变。

## 1. 运行

在项目目录执行，无网络、无数据库、无模型费用：

```powershell
Set-Location D:1code1TeyvatNotes
uv run python -m teyvatnotes.knowledge.agent_cli samples/questions/character.json --demo
uv run python -m teyvatnotes.knowledge.agent_cli samples/questions/quest.json --demo
uv run python -m teyvatnotes.knowledge.agent_cli samples/questions/clarification.json --demo
```

前两项为固定 `search → read → 摘录` 模拟，走真实工具校验、循环和引用检查；第三项条件不足，先追问且不生成。`provider=offline_demo`、`demo_only=true`、`model_called=false`；`generation_attempts` 是模拟端口尝试，不是付费请求数。虚构资料不是可用的原神攻略，不能把固定模拟演示当作真实模型选择工具的证明。

正式调用（可能计费，最多四次生成）：

```powershell
uv run --env-file .env python -m teyvatnotes.knowledge.agent_cli <问题JSON路径>
```

沿用 [模型配置](model-answering.md) 的 `LLM_*`，自行指定平台 HTTPS 地址和模型 ID；`LLM_API_STYLE=responses` 是当前支持的协议。不存在厂商/模型默认值或旧变量别名。使用业务 `DATABASE_URL`，不使用 `TEST_DATABASE_URL`，不自动建表、不回退内存。先导入人工审核、非虚构、完整、明确版本的资料并显式发布，再准备同版本的提问文件。不得把永久虚构样本改标为真实资料发布到业务库。

## 2. 工具契约

| 工具 | 必填参数 | 返回与权限 |
| --- | --- | --- |
| `search_knowledge` | `keywords`：1～12 个非空、至多 80 字符的字面词；`limit`：严格整数 1～5 | 命中 ID、章节、摘要、版本、分数；摘要不是完整证据。 |
| `read_evidence` | `evidence_id`：规范 UUID 字符串 | 只允许读取本次会话检索实际命中的 ID；返回最多 20 块库内上下文。 |

两个工具均禁止额外字段；重复 JSON 键、NaN、类型强制转换、bool 整数和超大参数被拒绝。模型不能传入版本、实体、条件、URL、SQL、文件路径或函数名来扩大权限。工具不是 `eval/getattr/import` 或 shell 的封装。

每次问答创建独立工具会话。正常范围固定为玩家确认的版本、类型、实体和适用条件，只读非虚构 `published`；演示只读虚构 `draft`，两者不混用。猜测一个存在但未由本次检索命中的证据 ID，仍被拒绝。知识端口还会重新检查回读范围。

搜索结果和预检证据不进入最终引用账本。只有成功回读、未截断、且输出未超预算的完整窗口可用于答案校验。回读仍是有限文本上下文，不保证完整父段落或所有任务步骤；工具出现截断时返回失败，不静默省略前置条件。引用地址由库内来源和定位生成，不采信模型生成的 URL。

## 3. 循环与停止条件

1. 校验玩家条件，复用原证据准备做只读预检；缺条件、无初始命中或预检截断时，不创建模型客户端、不读取密钥。
2. 给模型玩家字段、输出 Schema 和两个工具定义；预检窗口不交给模型。
3. 模型返回一个函数调用；程序校验名称、参数、证据能力，再执行。
4. 把稳定工具结果作为 `function_call_output` 返回，继续下一轮。
5. 模型给最终严格 JSON 时，复用已有引用、显式装备/角色持有子集和任务分节顺序校验。

原生协议依据 [OpenAI Function calling](https://developers.openai.com/api/docs/guides/function-calling)：发送 `tools` 和 `parallel_tool_calls=false`；用 `call_id` 关联结果。无状态 `store=false` 续接保留必要输出及加密 reasoning 项，只在本次内存驻留，不把它写进 CLI trace 或日志，不使用 `previous_response_id`。第三方返回不兼容格式时稳定失败，不换协议、供应商、模型或自动重试。

硬上限如下（应用调用可缩小，不能扩大）：

| 预算 | 默认/最大值 |
| --- | --- |
| 模型生成尝试 | 4 次 |
| 工具执行尝试（含失败） | 3 次，逐个执行 |
| 每次生成允许输出 | 2,048 Token |
| 单次 HTTP 总时限 | 沿用配置，默认 60 秒 |
| 检索预检＋循环用例总时限 | 90 秒 |
| 单工具执行时限 | 10 秒 |
| 每轮 UTF-8 编码后的请求预算（含工具 Schema 与历史） | 48,000 字节 |
| 单工具 JSON 输出 | 20,000 字符 |
| HTTP 响应 | 256 KiB |
| 已报告输入＋输出 Token 累计 | 20,000，获得报告后检查 |

Token 阈值不是付费硬上限：只有已报告用量可累计，检查发生在调用返回后，可能已经超出阈值且收费；达到阈值后不再执行后续工具/模型，最终答案恰好达到阈值可接受。未知用量不能当成零。费用保持 `null`，没有第三方价格核验。调用数量、字节和超时限制仍有效。总用例时限不包含 CLI 加载文件、创建依赖和释放连接池，不能称整个进程硬终止时间。

重复 `call_id`、每轮多调用、调用与正文混合均拒绝。预算用完后不再调用；末轮提出工具时不执行没有机会提交结果的工作。非法工具/参数和工具超时返回稳定错误，允许模型在剩余轮数内选择其他合法操作，失败也消耗预算；这不是网络重试或无上限修复。取消原样传播，清理 HTTP 资源并停止后续步骤，但不能保证供应商停止收费。

## 4. 输出与风险

输出沿用 `claims/questions/gaps/conflicts/used_player_conditions`，新增 `tool_attempts`、`tool_calls` 和 `model_turns`。模型轮记录服务端模型、已报告 Token、耗时和稳定错误；工具记录白名单名字、校验成功后的参数、耗时和错误。不回显非法原始参数、未知原始工具名、模型正文、reasoning、凭据或服务端错误正文。

工具参数可能含玩家关键词，完整输出也含玩家条件与最终答案。因此这只是私有 CLI 的单次返回，不自动持久化，也不进入现有公开读取 API 或中央日志；分享输出前应脱敏。并发调用各自持有历史、工具会话和引用账本。

模型调用失败、预算用完或最终答案拒绝时，`status=error` 且 CLI 退出码 1，没有被拒绝的结论；此前成功轮次的用量保留在 `model_turns`。累计 Token 只在所有尝试都已报告该项用量时返回总数，否则为 `null`。`model_called` 表示进入正式生成端口，不代表已计费、上游处理成功或底层模型身份已证明。

仍需人工检查证据是否支持结论、真实游戏版本/事实和正文隐藏的装备需求；确定性的 ID 和显式资源检查不等于语义可靠性。可选结构化输出开关仅用于旧单轮入口；工具循环的最终文本通过提示要求 JSON 和本地校验，不发送 `text.format`。原生工具和无状态加密 reasoning 在 OpenASI 上尚未付费实测，旧“你好”基础成功不覆盖这些兼容性。

后续独立扩展已提供 [RAG 检索底座](rag-retrieval.md)、[私有会话条件继承](conversations.md) 和 [50 题离线虚构流程评测与可靠性检查](evaluation.md)。会话每轮包装一个全新的工具用例，旧单问题 CLI 的行为和不持久化边界不变；可选回答事件仅为进程内元数据，不含文本增量。仍待完成：真实资料审核与网站采集、正式语义 Embedding、真实固定答案质量评测、任务步骤恢复/跨 attempt 费用控制和文本流事件。本轮不提交或推送 Git。

## 5. 本轮实际验证

- 全套 `539 passed in 14.67s`，其中 27 项真实 PostgreSQL 测试，没有跳过；旧测试重建仅发生在之前创建的专用可丢弃库，新知识测试使用随机 schema 并保留证据，不接触业务/原配置库。
- 本轮非数据库定向为 170 项：61 项工具边界、79 项 Responses 适配器、30 项循环/CLI；新增数据库链路为真实 PostgreSQL＋MockTransport 原生 Responses 的三轮检索/回读/答案，不访问模型服务商。
- Ruff 检查、117 文件格式检查、默认/Linux/Windows 各 86 源文件严格 mypy 和 Git diff 检查通过。
- 已实际运行角色与任务离线 CLI：3 轮模拟、2 次工具、带库内引用；缺条件样本为追问、0 轮、0 工具。
- 首次全测继承 `.env` 的 `api_only`，旧内存 API 用例不启动 Worker，任务不能按预期完成，后续事件测试等待；该次人工中断，不计为通过。重跑只在测试子进程清除部署 `AGENT_RUNTIME_*`、`OPENASI_*` 与业务 `DATABASE_URL`，保留指向专用库的 `TEST_DATABASE_URL` 后全套通过，没有改 `.env` 或任务代码。
- 本轮没有真实付费请求，也没有删除既有文件、数据库证据或提交/推送 Git。第三方原生工具调用与加密 reasoning 兼容仍待独立授权验证。
