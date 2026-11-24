# TeyvatNotes 架构设计

> 本文描述基于 Pi Agent 的重做版目标架构。交付范围和验收标准以 [软件需求规格说明书](requirements.md) 为准。

## 1. 系统边界

TeyvatNotes 通过 Docker Compose 部署在单台主机上，用户在主机终端进入容器中的 `teyvatnotes` CLI。Pi Agent 提供模型、工具循环和会话能力。TeyvatNotes 在其上实现原神攻略检索、联网补充、玩家条件与引用校验。V1 交付 CLI 容器，V2 加入 PostgreSQL、ChromaDB 和本机 Ollama 容器，V3 加入 Redis。DeepSeek 回答与联网搜索仍由外部 API 提供。

V1 用受控模型和工具验收 CLI、Pi 循环与会话恢复，不要求真实模型或正式原神问答。V2 首批仅做角色配队，含通用配队和按已确认库存配队，用一个标准角色和一个明确游戏版本验收。角色、版本与正式资料待确认；培养、配装、任务与解谜、机制问答不纳入 V2 首批。[需求概述](requirements.md#2-项目概述) · [待决项](requirements.md#85-待决项与建议)

```mermaid
flowchart LR
    User["主机终端"] --> CLI
    subgraph Stack["Docker Compose · 单主机"]
        subgraph App["teyvatnotes CLI 容器"]
            CLI["交互入口"] --> Pi["应用问答控制与 Pi Agent"]
            Pi --> Tools["攻略检索、回读和入库"]
            Pi --> Web["网络攻略适配器"]
            Web --> Tools
        end
        Pi --> Session["本地会话卷"]
        Pi --> Redis["Redis · V3"]
        Tools --> DB["PostgreSQL · V2"]
        Tools --> Vector["ChromaDB · V2"]
        Tools --> Embed["Ollama · Qwen3 Embedding · V2"]
    end
    Pi --> Model["DeepSeek 开放平台 · Flash"]
    Web --> SearchAPI["DeepSeek Search / Fetch API"]
    SearchAPI --> Source["已配置的网络来源"]
```

## 2. 组件与技术选型

| 组件        | 职责                                                      | 技术选型                                                                                                              |
| --------- | ------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------- |
| CLI       | 收集问题与补充条件，管理会话切换，展示工具活动、答复、引用和错误。提供攻略导入与维护入口。           | TypeScript、Node.js                                                                                                |
| 部署与编排 | 构建 CLI 镜像，管理依赖服务、容器网络及持久卷。 | Docker Compose，采用 `docker compose` 命令。 |
| Pi 接入层    | 嵌入 Pi 会话，处理模型调用、工具分派、上下文压缩和运行事件。                        | TypeScript、Node.js、Pi 官方 SDK `@earendil-works/pi-coding-agent`                                                    |
| 上下文缓存 | 按会话缓存已确认条件、历史压缩摘要等可重建的上下文数据。 | Redis（V3），通过 Node.js 客户端 `redis` 接入。[Redis 客户端文档](https://redis.io/docs/latest/develop/clients/nodejs/) |
| 问答控制与领域规则 | 管理本轮已确认条件、工具权限、预算和证据授权，校验最终回答。通过接口访问攻略存储和外部来源，支持离线独立测试。 | 应用内领域接口与校验逻辑                                                                                                      |
| 模型 | 根据本轮上下文与工具结果决定下一步，生成攻略答复。 | DeepSeek 开放平台，模型 `deepseek-flash`，通过 Pi 接入 OpenAI 兼容的 Responses API。 |
| 攻略库       | 保存攻略正文、版本、分块、发布状态、来源及引用位置，提供关键词筛选所需的业务数据。               | PostgreSQL                                                                                                        |
| 向量索引      | 保存分块向量、分块标识及粗筛所需的元数据，提供语义召回。                            | ChromaDB，TypeScript 使用官方 `chromadb` 客户端。 [Chroma 客户端文档](https://docs.trychroma.com/docs/run-chroma/client-server) |
| Embedding | 将攻略分块和查询文本向量化。 | 本机 Ollama，模型 `qwen3-embedding`，调用 `POST /api/embed`。[模型](https://ollama.com/library/qwen3-embedding) · [Ollama 接口](https://docs.ollama.com/api/embed) |
| 检索 | 按游戏版本、实体、攻略类型和适用条件缩小范围，融合两路候选并排序。 | PostgreSQL 专名与别名匹配、ChromaDB 向量召回、TypeScript RRF 融合。RRF 即 V2 的重排实现，不接入独立重排模型。 |
| 入库与联网资料源 | 从已配置的来源搜索并获取正文，将手工资料和网络正文交给同一入库流程。 | Markdown、JSON 与网络资料源适配器；V2 采用 DeepSeek 独立 Search / Fetch API。 |

选择 Pi SDK 和 Node.js 版本时，优先参考本机已安装且兼容项目的版本；没有可用版本时选择各自最新的官方免费稳定发行版并确认兼容性。镜像内固定实际使用的 Node.js 版本和 Pi SDK 依赖，容器部署在主机上无需另装这两项。 [Pi 官方 SDK 文档](https://docs.pi.dev/latest/sdk) · [DeepSeek 官方模型文档](https://api-docs.deepseek.com/quick_start/pricing/)

正式模型请求使用 DeepSeek 开放平台的 `https://api.deepseek.com`，调用 `POST /responses`。DeepSeek 的 Responses API 采用无状态请求。Pi 在请求的 `input` 中传递所需的消息、工具调用及工具结果；上下文以本地会话为依据，V3 可复用 Redis 中的派生数据。 [DeepSeek Responses API 指南](https://api-docs.deepseek.com/guides/responses_api/)

V2 的 Ollama 只负责本地 Embedding，回答仍由 DeepSeek 生成。后续如需将回答模型也迁到 Ollama，可通过 Pi 的模型配置接入无状态 `POST /v1/responses`，并验证所选模型的工具调用、工具结果续接与回答校验。 [Ollama OpenAI 兼容接口](https://docs.ollama.com/api/openai-compatibility)

上述是 V2 已选定的默认实现。回答模型、联网适配器与 Embedding 分别配置，替换其中一项需重跑对应验收，不改变攻略数据或引用边界。

### 2.1 项目模块级目录结构

以下是本项目的目标目录，按模块职责划分，不是当前仓库文件清单。目录随 §8 的交付阶段逐步落地，V1 不引入 V2 的检索与联网服务、V3 的 Redis 或 V4 的扩展依赖。

```text
TeyvatNotes/
├── package.json          # CLI 入口与 Node.js 依赖声明
├── tsconfig.json         # TypeScript 构建配置
├── Dockerfile            # CLI 镜像
├── compose.yaml          # 单机服务、网络与持久卷
├── src/
│   ├── cli/              # 命令解析、交互终端和结果展示
│   ├── bootstrap/        # 读取配置、装配用例与适配器
│   ├── application/      # 轮次、预算、工具授权、检索与入库编排
│   │   └── ports/        # 运行时、会话、存储、向量化、资料源与缓存接口
│   ├── domain/           # 术语、配队条件、适用范围、RRF 和引用规则
│   └── adapters/
│       ├── pi/           # Pi SDK、回答模型、本地会话提交与恢复
│       ├── postgres/     # 攻略原文、元数据、关键词查询与发布状态
│       ├── chroma/       # 向量索引与语义召回
│       ├── ollama/       # 本地 Embedding
│       ├── web/          # DeepSeek Search／Fetch、来源许可门禁及原文元数据提取
│       └── redis/        # V3 可重建的派生上下文缓存
├── migrations/           # 新版 PostgreSQL 表结构迁移
├── resources/
│   ├── terminology/      # 已确认的标准专名与别名
│   ├── skills/           # V4 受控加载的领域 Skills
│   └── extensions/       # V4 显式启用的可信扩展
├── tests/
│   ├── unit/             # 领域规则和应用用例
│   ├── integration/      # SDK、存储与服务适配器契约
│   ├── acceptance/       # CLI／Compose 端到端及 AT 用例
│   └── fixtures/         # 隔离测试用的固定资料与模拟响应
└── docs/
    ├── requirements.md   # 产品需求及验收标准
    ├── architecture.md   # 系统边界、技术选型与部署拓扑
    └── design.md         # 模块内部实现、接口与状态转换
```

依赖方向为 `cli → application → domain`。`bootstrap` 负责装配具体实现，`adapters` 实现 `application/ports`；应用和领域不直接依赖 Pi SDK、HTTP 客户端或数据库客户端。会话、数据库、向量索引和本地模型文件由 Compose 卷管理，不写入源码目录；凭据从运行环境注入。模块职责详见[详细设计 §1](design.md#1-模块与交付边界)，验收对应 NFR-005、IF-005 与 AT-11。

## 3. 问答与联网补充

V2 的查询范围以标准角色、明确游戏版本和配队目标为基础。通用配队不要求库存；按库存配队才检查必要的持有信息，明确为空不等于缺失。缺必要条件先追问；已确定的查询范围贯穿本轮。Pi 根据模型输出和工具结果决定下一步，应用在每次执行前检查权限、参数、查询范围和预算。

| 工具 | 开放条件与效果 |
| --- | --- |
| `search_guides` | 在本轮确认范围内查找已发布攻略，返回候选与摘要。明确区分命中、正常未命中和查询错误。 |
| `read_guide` | 仅回读批次开始前已回送、且属于本轮确认范围的候选。成功取得完整证据后才授予本轮引用资格。 |
| `search_web_guides` | 仅在正常未命中已经回送、本轮从未命中且尚未执行补查后开放。Pi 决定是否调用及搜索词，应用每轮最多执行一次；不得跟查库同批执行。 |

### 库内命中与未命中

1. **库内命中**。Pi 回读候选并使用完整证据。命中后关闭本轮联网入口；换词搜空或同批同时返回 `hit` 与 `miss` 都不会重新开放。回读截断可改读其他已授权候选，不能借此触发外搜。
2. **正常未命中**。两路检索均正常结束而无合格候选时才返回 `miss`，包括只有其他版本和正常过滤后候选为空。结果回送后，Pi 可在后续批次调用一次补查，也可直接结束并说明资料不足。条件不全先追问，任一路查询故障报错，均不算 `miss`。
3. **联网补充**。来源许可、最低元数据与索引就绪检查均通过后自动发布，不增加人工内容审核。Pi 在后续批次用原问题和范围重检，待候选回送后再回读并引用；本轮不再开放第二次补查。缺正文、缺元数据、索引故障或重检仍无匹配时返回对应缺口或错误。

同一批工具调用不得使用本批尚未回送的查库结果取得新权限。最终答复由应用校验结构、引用定位与显式持有条件；同范围证据冲突且无法消解时指出分歧，不强选一方。引用定位通过不等于原文支持结论，验收时分开检查。[轮次授权与批次校验](design.md#31-轮次授权状态与批次校验)

### DeepSeek 网络攻略适配器

联网资料源与回答模型分开配置。Pi 继续使用 `deepseek-flash` 的 Responses API；`search_web_guides` 由应用分派到网络攻略适配器，直接调用 `https://api.deepseek.com` 的独立接口，不启用模型内置联网工具。Search 和 Fetch 均使用 DeepSeek API Key 进行 Bearer 鉴权。[DeepSeek 网页搜索与抓取指南](https://api-docs.deepseek.com/zh-cn/guides/search/)

1. **先找链接**。`POST /v1/tools/search` 默认 `need_content: false`。适配器按许可配置构造 `site:` 等站点过滤，Pi 只能提供检索内容，不能改写过滤。只有上游能在读取正文前保证来源符合许可范围，才允许启用 `need_content: true`。
2. **先验来源，再取正文**。候选链接通过白名单与自动获取、保存许可校验后，才可调用 `POST /v1/tools/fetch`。未获许可的米游社链接仅作跳转线索，不请求或保存正文。搜索摘要和网页原始返回不作为引用。
3. **交给统一入库管线**。适配器将通过来源校验的可读取正文、原文链接及元数据交给入库管线，本轮分别计数 Search 与 Fetch。取得正文不等于可发布，发布也不等于可直接引用。

## 4. 攻略数据与检索

**入库。** 手工 Markdown、JSON 与联网取得的正文使用统一处理管线。联网自动发布要求可追溯的来源、原文链接、可定位的正文、明确的游戏适用版本、目标标准实体、攻略类型和适用条件。适用条件应有依据；缺失时保持未知，不从检索词、发布日期或模型推断。获准但缺最低元数据的正文可保留为待补全记录，不发布、不检索、不引用。

**版本与发布。** 同一来源、规范化内容与处理版本重复导入时复用已有版本；内容变化生成新版本，旧证据与引用位置保持可查。入库先在 PostgreSQL 保存不可检索的文档版本及分块，再生成向量并写入 ChromaDB。向量按分块 ID 和 Embedding 配置关联。来源和最低元数据合格、且索引确认可查询后，联网补充才自动发布。索引失败或未就绪则保持不可检索。维护者导入仍区分待发布、已发布和校验失败。网络读取、Embedding 调用与 ChromaDB 写入不占用 PostgreSQL 事务。

**本地向量化。** 入库分块和查询文本都通过本机 Ollama 的 `POST /api/embed` 向量化，与回答模型和联网资料源独立配置。固定使用的 `qwen3-embedding` 模型标签和向量维度，将具体配置与 ChromaDB 索引关联。切换 Embedding 模型或向量维度时，用新配置重建索引；确认可查询后再切换，不混用不同配置的向量。

**混合检索与引用。** 关键词通道用已确认的别名映射和 PostgreSQL，优先匹配角色、武器等精确术语。向量通道用 ChromaDB 按游戏版本、实体和攻略类型等元数据粗筛，再召回语义相近的分块。应用统一用 PostgreSQL 的发布状态、游戏版本和适用条件过滤两路候选。按分块 ID 去重后，TypeScript 用倒数排名融合（RRF）合并两路名次，不直接比较关键词分数与向量距离。[RRF 原论文](https://cormack.uwaterloo.ca/cormack/cormacksigir09-rrf.pdf)

候选命中用于授权回读，正文与引用统一从 PostgreSQL 回读。Ollama 向量化或 ChromaDB 查询失败属于检索故障，不算正常未命中，也不触发联网补查。

## 5. 会话、上下文与扩展

| 能力 | 架构边界 |
| --- | --- |
| 会话 | 复用 Pi 本地追加式会话。区分同一玩家全局库存、角色或版本专属条件、目标约束和任务进度。换角色、版本或目标不继承不适用的条件；旧引用可查看，不能直接用于新轮次。中断只保留已确认且已持久化的条件，不重放工具。 |
| 上下文 | 从会话、系统提示、已确认条件、本轮证据及相关 Skill 构建模型输入。V3 用 Redis 缓存派生数据，由 Pi 压缩历史，由应用检查 Token 预算、工具调用与结果的配对及证据范围。摘要涉及的模型调用计入预算。 |
| 活动与诊断 | CLI 订阅 Pi 的运行事件，展示轮次、模型、工具和终止原因。运行事件用于进度或诊断，会话记录用于恢复历史和重建上下文。 |
| Skills 与插件 | V4 从受控目录加载领域 Skills。显式启用可信插件，通过 Pi 扩展接口注册领域工具及监听事件。应用继续负责工具权限和证据范围。 |

### Redis 上下文缓存

本地会话是历史、确认条件和轮次状态的权威记录。应用按会话标识、所依据的会话事件位置和上下文配置版本定位 Redis 缓存，并设置可配置的过期时间（TTL）。Redis 保存可由本地记录重建的确认条件、压缩摘要及其对应的事件位置；Pi 和应用继续负责组装、裁剪和压缩，Redis 本身不执行这些操作。[Pi 上下文压缩参考](https://docs.pi.dev/latest/compaction) · [Redis 键过期文档](https://redis.io/docs/latest/commands/expire/)

历史或规则变更时不复用旧缓存。缓存缺失、过期或服务不可用时，从本地会话在预算内重建上下文，不重放历史工具调用。最终模型输入仍需检查预算和工具调用配对；本轮引用资格只来自本轮合法回读，不从 Redis 或摘要恢复。离线查看历史不依赖 Redis。

## 6. 预算、安全与故障处理

- 模型请求、合法工具执行、Search 和 Fetch 分别计数，共用从接收非空输入到终态的总时限；各类调用还受单步超时限制。分块、Embedding、索引可见及重检均计入本轮；被拒调用另记。实际用量和估算分开记录。补查基准见[需求 §8.4](requirements.md#84-v2-补查用例的预算口径)，具体上限待定。
- 模型工具调用须通过白名单、参数、调用标识和数据范围校验。Pi 通用编码工具和未登记扩展默认停用。取消、超时、不可恢复错误或预算耗尽后停止后续调用。失败轮次不得自动重发。
- 本地会话与 PostgreSQL 攻略资料分开存储。只将所需上下文发送给模型，只将必要搜索参数发送给网络来源。模型凭据从本地配置注入，普通诊断记录关联标识、阶段、耗时、用量与错误分类。网页、攻略和工具结果均作为外部数据处理。
- Redis 默认不对公网开放。缓存按会话隔离，不存模型密钥或完整会话日志；Redis 读写超时计入本轮预算。
- 旧资料与会话保留在原位置。只有另行批准迁移范围后才使用独立工具写入目标环境。

## 7. Docker Compose 部署边界

- **应用与依赖分开运行**。CLI 容器按需启动，保留终端交互；依赖容器在后台运行。CLI 通过 Compose 服务名访问依赖，默认不将服务端口映射到主机，不提供公网入口。[Compose 网络](https://docs.docker.com/compose/how-tos/networking/)
- **数据独立于容器**。Pi 会话、PostgreSQL 数据、ChromaDB 索引和 Ollama 模型分别挂载命名卷。Redis 用作可丢失缓存，停机或重建不会影响本地会话恢复。维护者导入目录单独只读挂载；凭据由部署环境以运行时密钥注入，不进入镜像或仓库。[Compose 数据卷](https://docs.docker.com/reference/compose-file/volumes/)
- **启动与就绪分开判断**。需要攻略问答时先启动 V2 核心依赖，并验证数据库初始化、索引和指定 Embedding 模型已就绪。仅查看历史时不要求依赖容器或 DeepSeek 可用，Redis 也不是 CLI 启动条件。
- **旧部署保持隔离**。新版 Compose 只编排新版 CLI 和所需依赖，使用新的项目和数据卷，不接管旧桌面服务的数据库或卷。配置、命令和停机步骤见[详细设计](design.md#8-compose-部署设计)。

## 8. 分阶段交付

| 阶段 | 实现边界与验收 |
| --- | --- |
| V1 Pi 接入 | Compose 运行真实 CLI 和 Pi SDK；用受控模型验收工具循环、权限、停止及会话恢复。不依赖真实模型或 V2 攻略库。对应 AT-01 至 AT-04、AT-11。 |
| V2 攻略 RAG | 用首批配队范围在隔离 Compose 环境验证两路召回、RRF、元数据与索引发布门禁、会话条件继承、引用定位及原文支持。用模拟 Search／Fetch 必验补查闭环和预算；真实获准来源演示是否作为放行门槛待定。对应 AT-03 至 AT-06、AT-10、AT-11。 |
| V3 上下文 | 加入 Redis 服务，覆盖长会话预算、压缩、缓存命中、失效和服务停机后的重建。对应 AT-07、AT-11。 |
| V4 Skills 与扩展 | 验证按需加载攻略 Skill、通过 Pi 扩展登记工具及监听事件，并验证权限和诊断脱敏。对应 AT-08。 |
| V5 增强 | 按选定范围交付流式交互、运行中引导、重试、追踪、评测或外部扩展。对应 AT-09。 |

正式角色与版本、资料权限、正式题集和质量阈值、是否把真实联网演示设为放行门槛，以及预算数值，遵循[需求 §8.5](requirements.md#85-待决项与建议)。Python 模块复用、具体 Embedding 标签与维度、分块与召回参数、压缩阈值、终端支持和旧数据迁移留待后续细化。
