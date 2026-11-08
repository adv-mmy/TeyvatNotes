# 通用 Responses 接入与单轮带引用问答

更新：2026-10-04。对应学习建议 13.11 第 2 项的实现：直接 HTTP 模型接入、关键词证据准备、结构化答案及程序校验。不是工具调用循环、完整 RAG 或已验证的原神攻略产品。

## 1. 本机配置

配置文件为项目根目录 `D:1code1TeyvatNotes\.env`，不是 `.env.example`。保留已有数据库设置，追加：

```dotenv
LLM_API_STYLE=responses
LLM_API_KEY=填入服务商提供的密钥
LLM_BASE_URL=https://llm.example.com/v1
LLM_MODEL=服务商提供的模型ID
LLM_TIMEOUT_SECONDS=60
LLM_STRUCTURED_OUTPUTS=false
```

`.env` 已被 Git 忽略；`.env.example` 只能保留占位符。程序不回显密钥。只读取 `LLM_*`，没有 `OPENASI_*` 或 `OPENAI_API_KEY` 别名、厂商/模型默认值或自动回退。上面的域名只是占位示例，必须替换。地址可为 API 基址或完整 `/responses` URL（可有自定义路径、HTTPS 端口）。当前只实现 Responses 协议；Chat Completions、Anthropic 等不同协议需要另写适配器，不能只改配置名。Python 模块本身不自动加载 dotenv，以下命令用 `uv run --env-file .env` 显式加载；服务端部署则注入环境变量。已经出现在聊天里的密钥应撤销并轮换，不能继续使用。

在项目目录，配置好新密钥后可主动执行一次真实请求（可能计费）：

```powershell
uv run --env-file .env python -m teyvatnotes.models.probe
```

该命令只发送“你好”，最多允许 128 个输出 Token，不上传资料或本机文件；成功时输出服务端模型名称、用量和耗时，不回显密钥或原始响应。它不验证知识库、引用质量或所有可选 API 参数。

正式攻略问答还需要独立设置业务 `DATABASE_URL`、显式执行 `alembic upgrade head`，并导入人工审核后的非虚构、明确版本、完整资料且显式 `--publish`。不能把 `TEST_DATABASE_URL` 直接作为业务库，也不能将演示样本改标为真实资料以绕过限制。

## 2. 离线演示与正式入口

无密钥、无数据库、无费用的演示：

```powershell
uv run python -m teyvatnotes.knowledge.answer_cli samples/questions/character.json --demo
uv run python -m teyvatnotes.knowledge.answer_cli samples/questions/quest.json --demo
uv run python -m teyvatnotes.knowledge.answer_cli samples/questions/clarification.json --demo
```

`--demo` 只从临时内存中的纯虚构草稿摘录，模拟模型不访问网络、数据库或模型凭据。输出 `provider=offline_demo`、`demo_only=true`、`model_called=false`；`generation_attempts` 表示模拟生成尝试，不是付费请求数。虚构 URL 是样本身份，不代表可访问的真实来源。这不是 GPT 的回答或真实游戏质量验收。

正式使用自己准备的问题文件：

```powershell
uv run --env-file .env python -m teyvatnotes.knowledge.answer_cli <问题JSON路径>
```

输入沿用 `knowledge/query_cli.py` 的问题格式。角色回答额外检查 `owned_equipment`；配队目标为“配队”时还需 `owned_characters`。例如 `"owned_equipment": ["已拥有的装备名"]`，`null`/省略表示未知，`[]` 表示明确没有。不要把资料的适用条件当作完整玩家库存；两个字段需分别确认。实体、版本、关键词仍是显式输入，不自动理解别名或分词。

## 3. 执行与输出

1. 输入字段校验，必要信息不足返回 `needs_clarification`。
2. 只读检索准确版本、实体和已确认条件下的正式已发布证据；无命中或上下文截断返回 `insufficient_evidence`。
3. 去重并完整装入有界上下文；提示数据超过 40,000 字符时不截断关键步骤、不调用模型。
4. 证据就绪后才创建模型客户端、读取密钥。一次问答最多调用一次模型，没有自动重试、输出修复重试、供应商切换或降级到演示。
5. 严格解析 JSON，禁止多余字段和模型提供的结构化链接。每项结论必须有现存证据 ID；程序生成 `citations` 中的来源链接、时间/段落和版本。
6. 对显式 `required_equipment/required_characters` 检查玩家持有子集；任务必须具有前置、有序步骤和完成标志的分节顺序，缺失或乱序会拒绝整个答案。

成功输出为 `answer`、`needs_clarification` 或 `insufficient_evidence`。发生模型、预算或答案校验错误时为 `error`，CLI 退出码 1；追问/资料不足属于正常结果，退出码 0。

关键结论放在 `claims`，逐项标注 `fact/recommendation/inference` 和 `summary/prerequisite/step/completion/alternative`，并附引用。`used_player_conditions` 仅来自输入，`questions/gaps/conflicts` 分别展示追问、缺口和矛盾；不保存会话。`generation_attempts` 记录生成端口尝试；`model_called` 在正式模式表示已进入真实生成适配器，不表示供应商已处理或计费成功。

记录成功返回的 `input_tokens/output_tokens`、服务端模型名、用例 `elapsed_ms`。未知用量为 `null`，不是零；已生成但被引用校验拒绝时仍保留已报告用量。HTTP 失败、超时/取消时可能已收费但无法获知用量。没有配置平台的已核验价格与币种，所以 `cost=null`；不能拿 OpenAI 官方价格计算第三方账单。

## 4. HTTP、安全与兼容性

客户端使用显式配置的 HTTPS 主机、端口和 API 路径，不限制服务商域名或模型 ID。API 基址自动追加 `/responses`，完整端点不重复追加。拒绝带用户信息、空主机、非法端口、查询参数、fragment、空白字符及已知不同协议的完整端点；不跟随重定向、不读取代理环境、不自动重试，TLS 证书校验保持开启。每次调用创建并关闭 HTTP Client。发送模型和服务端返回模型必须与本次 `LLM_MODEL` 精确一致；没有全局模型常量或别名自动放行。

HTTP 超时默认 60 秒、配置范围 1～120 秒，同时有覆盖连接及读取的总超时；问答用例另有覆盖检索与生成的 90 秒总时限。后者不覆盖 CLI 的文件加载、依赖创建及最终连接池释放，不能称整个进程的硬终止时限。响应正文最多 256 KiB，解析后文本最多 32,000 字符，输出上限 4,096 Token。

协议处理依据 [OpenAI 文本生成规范](https://developers.openai.com/api/docs/guides/text)：原始 HTTP 响应从 `output` 中遍历 assistant message 的 `output_text`，不假定首项就是正文，也不依赖 SDK 的快捷属性。拒绝非完成状态、拒答、缺失正文及与所选模型不同的服务端模型标识。未获供应商确认的模型别名不会静默放行。

默认以提示词要求 JSON 并在本地严格校验。`LLM_STRUCTURED_OUTPUTS=true` 才发送 [Responses 的 text.format JSON Schema 格式](https://developers.openai.com/api/docs/guides/structured-outputs)，不使用 Chat Completions 的 `response_format`；不兼容时直接报错，不再发第二次付费请求。`store=false` 是请求参数，不是第三方无保存/无训练的保证。

私有会话的显式 `--extract-model` 也复用这个普通生成客户端和配置，使用独立的闭合提取 Schema；工具循环的原生函数调用适配器不受此开关影响。模型辅助提取需另计一次调用，不改变本文件中单轮回答入口的一次生成契约；联合预算与取消/竞争统计见[会话说明](conversations.md#显式模型辅助提取)。提取的第三方 strict 兼容尚未真实验证，不自动重试或降级。

错误只输出稳定代码，不输出密钥、底层异常或服务端错误正文：

| 场景 | 公开代码 |
| --- | --- |
| 未提供密钥/非法配置 | `model_key_not_configured` / `invalid_model_configuration` |
| HTTP 401/403、429、5xx | `model_authentication_failed` / `model_rate_limited` / `model_unavailable` |
| 其他非 200（含重定向） | `model_request_rejected` |
| HTTP/总读取超时、问答用例超时 | `model_timeout` / `answer_timeout` |
| 不完整、拒答、超大正文、格式非法 | `model_incomplete` / `model_refused` / `model_response_too_large` / `invalid_model_response` |
| 虚构引用、未知装备/角色需求、非法答案 | `invalid_answer_citation` / `answer_equipment_not_owned` / `answer_character_not_owned` / `invalid_answer_structure` |

合作取消传播至 HTTP Client，清理资源且不发后续请求；这不保证供应商已停止计算或计费。

## 5. 当前验收边界

初始开发验收使用 MockTransport 和可控模拟客户端，没有真实模型调用；不能用离线测试确认任何第三方平台的身份、模型实际能力、参数兼容、隐私保留、价格或真实答案质量。旧 Terra 失败探测见第6节；用户改选 Sol 后的一次基础生成成功见第7节。后者不是实际攻略问答或完整答案质量验收。

引用存在性不等于证据支持结论；显式资源列表子集检查无法识别模型在自然语言中隐瞒的资源需求或错误分类。分节顺序不等于游戏步骤在语义上正确。`claims.text/questions/gaps` 是模型生成文本，可能包含不受信任的链接；可信引用仅指程序生成的 `citations`。输出始终带事实/语义支持尚未验证警告。

后续新增的独立工具循环入口见 [最小只读工具循环](tool-agent.md)，条件与答案持久化见 [私有会话](conversations.md)；本文件中的单轮入口仍保持一次生成，不保存会话。仍待实现：真实版本/角色/任务的审核样本、正式语义检索、50 条固定评测、跨 attempt 的费用预算与流式事件。Task API、Worker、SSE 和两种确定性任务没有修改；私有会话并未提供公开用户鉴权，不能把玩家回答接到现有公开读接口。

通用配置改造使用多域名、多模型 MockTransport 验证，无本轮真实生成。下面第6～7节是改造前的历史 OpenASI/Terra/Sol 记录，其中旧变量名和常量约束不代表当前实现。当前源码为 `models/responses.py` 与 `models/responses_tools.py`；响应模型与本次配置值精确核对，不默许别名替换。正式输出的 `provider=responses_compatible` 是适配器类别，不是已验证的服务商身份；离线演示仍为 `offline_demo`。

## 6. 旧 Terra 真实接口探测记录（2026-10-03，保留历史）

用户确认本机密钥已填写后执行验证，不记录或回显密钥、授权头及原始响应：

| 检查 | 实际结果 |
| --- | --- |
| DNS 解析 | 成功 |
| TLS 证书验证 | 成功，未关闭证书校验 |
| 无凭据 GET /v1/responses | HTTP 401，证明 HTTPS 接口可达 |
| 带本机凭据 GET /v1/models | HTTP 200，返回列表包含 gpt-5.6-terra |
| 第一次 probe 生成请求 | model_unavailable，未获得模型响应 |
| 一次有界诊断生成请求 | HTTP 503，服务端 error.type 为 api_error，未获得模型响应 |

两次生成尝试均仅发送“你好”，各请求允许最多128个输出Token，没有上传攻略资料、玩家内容或本机文件。诊断请求与初次请求采用相同生成参数，是人工定位步骤，不是客户端自动重试；诊断后停止请求，没有供应商/模型回退。

鉴权和模型目录可读不等于该账号可以成功生成，也不证明供应商的底层模型身份。HTTP 503 是本次生成接口实际返回，现有脱敏信息不足以区分服务拥塞、上游故障、账号/模型路由或其他服务端原因；不要推测具体根因，更不要关闭TLS验证、改用已泄露密钥或高频重试。

旧 Terra 的这两次探测没有成功报告的Token用量，费用未知，不声明零费用或未收费。当时需要供应商侧排查生成接口/账号路由；旧 Terra 的结论为网络与基本鉴权探测成功，真实生成与攻略回答质量验收未通过，不覆盖后续 Sol 的结果。

随后按用户要求再重试一次，仍为 HTTP 503。用户提供的平台详情明确指出：`gpt-5.6-terra` 在 `CodeX专用 (distributor)` 分组没有可用通道。上述记录属于旧 Terra，不是 Sol 的失败记录；模型目录可见不能代替分组路由可用性。

## 7. 用户明确切换 Sol（2026-10-03）

用户指定准确模型 ID `gpt-5.6-sol`，仅切换模型常量、本机 OPENASI_MODEL 单行和当前配置示例，不修改密钥、Base URL、协议、提示词、生成预算或 Task API/Worker。旧 Terra 的试验记录保留，不全局替换历史。

官方模型信息参考 [OpenAI GPT-5.6 Sol 文档](https://developers.openai.com/api/docs/models/gpt-5.6-sol)，它不能证明 OpenASI 的模型身份、路由、参数兼容或价格。当前接口可用性需用本机已配置凭据执行一次有界调用；攻略事实与引用支持仍须独立验收。

切换后的实际验证：

| 项目 | 结果 |
| --- | --- |
| 本机实际配置及请求模型 | gpt-5.6-sol |
| 真实生成尝试 | 1次，只发送“你好”，输出上限128 Token，无自动重试 |
| HTTP状态 / 响应校验 | 200 / 成功，非空文本且终态completed |
| 服务端报告模型 | gpt-5.6-sol，与请求精确一致 |
| 服务端报告Token | 输入4387，输出13 |
| 本机测量耗时 | 3690ms |
| 费用 | 未核验供应商价格，保持null |

平台报告的4387输入Token不是本地“你好”的Token估算，无法据此推断平台如何构造上游输入；没有查看或回显原始模型正文/密钥，没有上传本机文件。基础调用成功不能证明第三方底层模型身份、text.format支持、所有参数兼容或攻略结论正确。

切换后全套测试为 `368 passed in 13.72s`，其中26项真实隔离PostgreSQL测试；新增1项明确Sol字面值与旧Terra拒绝测试，防止共享常量的假响应与请求一起保留旧模型却自洽通过。Ruff检查、108文件格式检查、默认/Linux/Windows各81源码strict mypy及diff检查通过。未修改Task API、Worker或数据库结构，未提交或推送Git。
