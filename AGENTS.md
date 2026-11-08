# AGENTS.md

<INSTRUCTIONS>

## 一、通用约束

- 回答、代码注释和输出文件尽可能使用简体中文；中文文件统一使用 UTF-8 编码。
- 即使具备完全访问权限，删除任何文件前也必须先获得用户批准。
- 修改前先检查 `git status` 和相关文件，保留用户已有变更；不得用重置、覆盖或清理工作区的方式处理无关改动。
- 未经用户明确要求，不执行 Git 提交、推送、变基、打标签或创建 PR。
- 只做当前任务需要的修改。不要顺手升级依赖、重排无关代码、引入新基础设施或提前实现后续周内容。
- `D:\code\Codex_function_teaching\Agent开发技术栈学习路径建议.md` 是阶段目标与学习顺序的依据；实际代码、测试和 `pyproject.toml` 是当前行为的事实来源。两者不一致时，必须区分“已实现”和“规划中”，不得把规划描述成现状。

### 代码原位更新与交付包替换

- 每次更新代码，应直接修改原有函数、方法和模块中的实现，复用现有入口；不得为同一功能另建接口、版本化入口或平行实现，也不得以 `_v2`、`new`、`legacy` 等命名绕过原位修改要求。
- 只有用户明确要求新增或替换接口时才新增接口；替换时同步修改所有项目内调用方、测试和文档，移除被替代的旧接口及其兼容包装、别名、转发和回退逻辑，不保留新旧版本并行的兼容性接口。
- 生成新的代码包或结果包时，直接更新或替换原有交付文件、压缩包或文件包，沿用原有路径、文件名和目录结构；不得为了保存新版另建带日期、版本号或批次号的文件夹，也不生成旧版副本。首次生成时使用约定的固定位置，后续始终在该位置更新。
- 替换范围仅限本次任务对应的代码包和结果包，不覆盖无关用户文件。若替换确实需要删除文件或目录，仍须先获得用户批准；可以原位写入的文件直接更新。
- 本节的文件包替换要求不改变数据库中资料版本、证据、检查点及审计历史的保留规则；这些业务记录继续遵守相应的数据不变量。今后的代码更新和交付包生成以本节为准。

## 二、项目定位与当前基线

- 本项目是阶段一“Python 后端底座”的长期主项目，目标是先用确定性任务学习 Agent Runtime 同样需要的状态、并发、失败恢复、持久化和可观测性。
- 阶段一禁止自行接入 LLM、Agent 框架、RAG 或任意函数/系统命令执行。只有用户明确要求进入相应阶段时才能引入。
- 当前基线为 `0.8.1 / 第 8 周 + 第四周 PostgreSQL 补全`：Python 3.12、FastAPI、Pydantic v2、进程内 `asyncio` Runtime、PostgreSQL 持久化与独立 Worker、可观测性、安全部署、CI、七场景演示、并发基线、架构图和恢复手册。
- 当前只允许两种确定性任务：
  - `sum_numbers`：`numbers` 含 1～100 个严格整数，每项范围为 `-1_000_000～1_000_000`；
  - `word_count`：`text` 为 1～10,000 字符的非空白字符串。
- 未设置 `DATABASE_URL` 时默认使用 `InMemoryTaskRepository`；设置后使用 `PostgresTaskRepository`。`api_only` 只持久化命令，独立 `worker_main.py` 通过 `PostgresWorkerRepository` 原子领取、持久化租约并恢复过期任务。Compose 运行 API 与 Worker 两个进程；教学用 `main.py` 仍保留 embedded 模式。
- 当前默认运行时配置为 `worker_count=4`、`max_concurrency=2`、`task_timeout_seconds=30`、`shutdown_timeout_seconds=5`。

## 三、架构和依赖方向

必须保持以下主调用链和单向依赖：

```text
HTTP / FastAPI
  → Pydantic Schema 与显式转换
  → TaskService 应用用例
  → Task 领域状态机
  → TaskRepository 端口及其实现

创建 / 重试
  → TaskRuntime
  → asyncio.Queue
  → 固定数量 Worker
  → Semaphore 并发门控
  → TaskExecutor
  → Repository 原子状态转换

API-only 进程
  → TaskCommandHandler
  → Repository 只持久化命令

独立 Worker 逻辑
  → WorkerRepository.recover_expired()
  → WorkerRepository.claim_next()
  → TaskExecutor
  → complete_claim() / fail_claim()

SSE 事件流
  → TaskEventService 按 id > Last-Event-ID 读取
  → StreamingResponse 编码 id/event/data
  → 无事件发送心跳，终态排空后关闭

可观测性
  → ContextVar 关联 request/task/attempt/worker
  → JSON Log + MetricsRegistry
  → live/ready 分离的健康检查

写接口安全
  → AppSettings 环境变量校验
  → Bearer API Key 恒定时间比较
  → 创建/重试进程内滑动窗口限流
```

- `domain/`：只放任务实体、值对象、枚举和领域异常；不得导入 FastAPI、Pydantic、数据库、Runtime 或 LLM SDK。
- `application/`：定义用例、应用模型、应用异常和 `TaskRepository` Protocol；不得感知 HTTP 或具体存储实现。
- `repositories/`：实现应用层端口。内存实现必须保持线程安全、复制隔离和原子状态转换。
- `runtime/`：负责任务入队、执行、并发限制、超时、取消传播和启停；不得把 HTTP 响应逻辑写入运行时。
- `api/`：负责输入校验、依赖注入、调用应用服务或 Runtime、响应映射和异常到 HTTP 的转换；不得在路由中复制状态机规则。
- 请求 Schema、应用命令、领域对象和响应 Schema 必须分开，通过显式函数或构造过程转换；不要直接暴露领域对象，更不能在后续直接暴露 ORM 对象。
- Repository 和 Runtime 由 `create_app()` 创建后放入 `app.state`，同一应用实例的请求共享它们；测试中的每个应用实例必须使用独立依赖，避免数据和协程跨测试污染。

## 四、必须保持的领域与运行时不变量

### 任务状态机

只允许以下转换：

```text
queued → running → succeeded
queued/running → cancel_requested → cancelled
running → failed → queued
```

- `succeeded` 和 `cancelled` 是终态，不能再转换。
- `Task.status` 必须保持只读；所有状态变化统一经过 `Task.transition_to()`，非法转换抛出 `InvalidTaskTransition`。
- 一次状态转换同时替换本次结果；进入没有结果的状态时要清除旧结果，重试后不得残留上次失败信息。
- API 或 Runtime 需要执行取消、重试时，应调用已有状态机/应用服务，不得直接赋值 `_status` 或另写一套合法性判断。

### Repository

- `transition()` 必须在一个临界区内完成“读取、校验、转换、保存”，并发竞争只能有一个合法结果。
- 内存 Repository 写入和返回时保留 `deepcopy` 隔离，调用方不能通过返回对象绕过仓储修改已存任务。
- PostgreSQL 状态转换必须用行锁串行化同一任务，并在同一事务中写入 `tasks`、`task_attempts` 和 `task_events`；幂等键的并发唯一性必须交给数据库约束保证。
- 列表顺序稳定为 `created_at` 后接 `id`；先按状态过滤，再计算 `total`，最后执行 `offset/limit` 分页。PostgreSQL 中的总数与分页查询必须共享同一事务快照。

### 异步 Runtime

- `worker_count` 表示领取队列任务的固定 Worker 数；`max_concurrency` 表示实际进入执行器的并发上限，两者含义不得混合。
- 创建和重试必须在“仍接收任务”的检查与入队之间保持原子性；运行时未启动或正在停止时返回 `RuntimeNotAcceptingError`，由 API 映射为 `503`。
- 每次执行使用独立 `attempt_id`；同一业务任务重试时不能与旧 attempt 的 `_pending`、`_active` 状态互相覆盖。
- 每次 `queue.get()` 必须恰好对应一次 `queue.task_done()`。新增异常或取消分支时仍要保证 `queue.join()` 可以收敛。
- 执行器必须是可合作取消的异步实现；清理后继续传播 `asyncio.CancelledError`，不要吞掉取消。
- 任务超时落为 `failed` 并保存稳定的公开错误；客户端取消 queued/running 任务时先落为 `cancel_requested`，清理结束后落为 `cancelled`。
- 停机顺序保持为：先停止接收新任务，再等待已有任务；超时后请求取消并显式收敛 Worker、执行协程、队列计数和任务状态。不得静默丢弃已接受任务或遗留协程。
- 不得在事件循环中直接执行阻塞 I/O 或 CPU 长任务；确需引入时，应明确采用线程、进程或外部 Worker，并补相应并发和停机测试。

### 独立 Worker 策略

- `claim_next()` 必须原子完成选择、`queued → running`、attempt 创建和租约所有权记录；两个 Worker 不得同时拥有同一任务的有效租约。
- 租约过期后旧 Worker 不得提交成功或失败结果；新 Worker 应把它记录为 `lease_expired` 并在最大尝试次数内恢复。
- 永久错误不自动重试；可重试错误、超时和未预期错误使用带上限抖动的指数退避，单轮自动执行不超过 `max_attempts`。手动重试开启新一轮预算，全局 `attempt_number` 仍持续递增。
- Worker 被强制结束时不伪造完成记录，由租约过期机制恢复；因此只能保证至少一次执行，执行器必须考虑业务幂等。
- Worker 执行协程收到 `CancelledError` 时，任务与 attempt 保持 `running` 等待租约恢复；日志记录 `task_abandoned`，不得记录 `task_completed/cancelled`，也不得增加 completed/cancelled 指标。
- 第五周不得把 SQL、索引、数据库事务或 Alembic 当作本周新内容；这些属于第四周，待 PostgreSQL 环境就绪后单独补完和验收。

### 事件与可观测性

- 任务创建和每次状态变化必须产生递增 ID 事件；当前确定性执行的进度事件为 0% 和 100%。
- `Last-Event-ID` 是已确认游标，重连只能返回严格大于它的事件；分页排空前不得因终态提前断流。
- 任务未终止且暂无事件时发送 SSE 心跳；客户端断开或终态事件排空后必须停止生成器。
- 请求、任务、attempt 和 Worker 使用 `request_id/task_id/attempt_id/worker_id` 关联；ContextVar 作用域退出后必须恢复，不得串扰后续任务。
- 日志只记录白名单结构化字段；不记录 payload、查询参数、请求体、异常原文、原始未匹配 URL、密钥或凭据。
- liveness 只表示进程存活；readiness 必须有界检查存储，PostgreSQL 模式需验证 `tasks/task_attempts/task_events` 可访问，内嵌 Runtime 还必须处于 accepting。依赖失联或表未迁移时返回稳定 `503/not_ready`，不泄漏内部异常。
- `MetricsRegistry` 只是单进程瞬时快照；必须明确它不持久、不跨进程聚合，不得把它描述成生产级 Metrics/Trace 后端。
- 独立 Worker 的监控应用与 TaskWorker 共享同一 Registry，默认只监听回环地址 9101。Worker readiness 必须同时确认循环正在运行和数据库表可用；API readiness 不能替代 Worker 检查。不得在该私有监控应用增加业务任务接口。

### 安全、配置与容器

- `AGENT_RUNTIME_API_KEY` 必须由部署环境提供，不得在源码、Compose、镜像层、Git 或日志中写入真实值；示例只能使用明显占位符。
- 创建、取消和重试必须通过 Bearer API Key 鉴权；读取、SSE、存活、就绪和当前学习用指标保持公开。修改鉴权范围时同步更新 OpenAPI、README 和测试。
- Bearer 凭据只用于恒定时间比较，不得作为主体 ID、日志字段、限流键、异常文本或响应内容保存。客户端自定义请求 ID 同样不得直接进入日志。
- 创建与重试属于高成本接口，共享线程安全的进程内滑动窗口；限流只适用于单进程，不得暗示跨容器一致性。
- 任务类型和 payload 必须继续由枚举、判别联合与 `extra="forbid"` 白名单约束；阶段一禁止用户选择 Python 函数、模块、系统命令或任意执行器。
- 应用容器以非 root 用户运行，文件系统只读，通过 `/health/ready` 检查就绪，并让 SIGTERM 进入 FastAPI lifespan 完成优雅退出。
- 镜像默认使用 `uvicorn api_main:app`，强制 API-only。HTTP/SSE graceful shutdown 上限为 5 秒，必须短于 API 容器 15 秒宽限期；改动时覆盖非终态 SSE，不能只用空闲停机证明收敛。实际 Docker 验收记录位于 `docs/docker-acceptance.md`。
- PostgreSQL 使用命名卷；迁移由一次性 Compose 服务显式执行。Compose 的 API 使用 `api_only`，独立 Worker 使用 `FOR UPDATE SKIP LOCKED`、部分唯一索引和租约字段协调并发。

### CI、演示与故障恢复

- CI 必须分开运行代码质量/单测与真实 PostgreSQL 集成测试；真实库 Job 先等待数据库健康、执行 Alembic 迁移，再运行 `postgres` 标记测试，不得用 skip、`continue-on-error` 或 `|| true` 伪造通过。
- CI Action 与 uv 使用明确版本，权限保持 `contents: read`；测试数据库只能使用隔离的非生产凭据。
- 并发基线必须先验证全部任务正确收敛，再报告耗时和吞吐；共享 Runner 上不设脆弱的固定性能阈值，不把内存结果描述成生产容量。
- 阶段一演示必须覆盖正常、幂等、取消、超时重试、Worker 中断、租约恢复、SSE/日志关联七个场景，并返回机器可检查的报告。
- 默认七场景演示验证内存租约策略；`scripts/postgres_recovery_demo.py` 和 `postgres` 集成测试验证跨 Engine 的 PostgreSQL 租约恢复，两者的边界必须分别说明。
- `scripts/process_acceptance_demo.py` 用隔离随机 schema 验证本机真实 API/Worker 进程强杀、数据库真实租约、SSE/日志/Worker 指标；schema 留存，不消费原队列。Linux 容器信号、镜像和命名卷须另行验收，不得把本机进程演示称为 Docker 验收。
- 故障手册默认保留数据和证据；不得把删除卷、删表、改写状态或泄漏密钥当作常规恢复步骤。

## 五、当前 HTTP 契约

| 方法 | 路径 | 成功行为 | 主要失败 |
| --- | --- | --- | --- |
| `POST` | `/v1/tasks` | Bearer 鉴权并限流；`201`，返回初始 `queued` 任务和 `Location`；可携带 `Idempotency-Key` | `401`、`422`、`429`、`503`、`500` |
| `GET` | `/v1/tasks/{task_id}` | `200`，只读查询 | `404`、`422`、`500` |
| `GET` | `/v1/tasks/{task_id}/events` | `200 text/event-stream`，支持 `Last-Event-ID` | `404`、`422`、`500` |
| `GET` | `/v1/tasks?status=...&offset=0&limit=20` | `200`，过滤后分页 | `422`、`500` |
| `POST` | `/v1/tasks/{task_id}/cancel` | Bearer 鉴权；`200`，请求取消 | `401`、`404`、`409`、`422`、`500` |
| `POST` | `/v1/tasks/{task_id}/retry` | Bearer 鉴权并限流；`200`，失败任务重新入队 | `401`、`404`、`409`、`422`、`429`、`503`、`500` |
| `GET` | `/health/live` | `200`，进程存活 | - |
| `GET` | `/health/ready` | `200`，依赖就绪 | `503/not_ready` |
| `GET` | `/metrics` | `200`，进程内 attempt 快照 | `500` |

- 创建接口只负责持久化和入队，不等待任务执行完成；Worker 随后推进状态。
- `GET` 请求不得改变任务状态或时间戳。`offset >= 0`，`1 <= limit <= 100`，`total` 表示过滤后、分页前的总数。
- 输入任务类型必须来自显式白名单；Pydantic 模型使用 `extra="forbid"` 拒绝多余字段，并为长度、范围和类型设置边界。
- 所有公开错误统一为 `code/message/details/request_id`，并在响应头返回相同的 `X-Request-ID`。
- 固定映射：鉴权失败为 `401/authentication_required`，不存在为 `404/task_not_found`，状态冲突为 `409/invalid_task_transition`，校验失败为 `422/validation_error`，超限为 `429/rate_limit_exceeded`，未预期异常为 `500/internal_error`，运行时不可接收任务为 `503/runtime_unavailable`。
- 不得把 `str(exc)`、堆栈、文件路径、密钥或内部实现细节返回给客户端；真实异常仅进入受控日志。
- 路由变化必须同步核对响应模型、状态码、错误声明、`/docs` 和 `/openapi.json`。

## 六、目录职责与常见变更联动

- `src/teyvatnotes/domain/task.py`：修改状态、转换或结果语义时，同步更新领域、Repository、API 和 Runtime 相关测试。
- `src/teyvatnotes/application/`：更新用例时优先修改原有函数并复用应用模型/端口；只有用户明确要求新增接口时才扩展端口，再由 API 或 Runtime 调用；不要让路由越过应用层操作仓储。
- `src/teyvatnotes/repositories/memory.py`：修改并发或复制策略时，必须覆盖重复 ID、对象隔离、原子转换和并发竞争。
- `src/teyvatnotes/repositories/postgres.py`：修改写路径时必须检查事务原子性、行锁、attempt/event 一致性、幂等冲突和稳定分页。
- `src/teyvatnotes/database/` 与 `migrations/`：ORM 模型、迁移、约束和索引要保持一致；不在应用启动时自动改表。
- `src/teyvatnotes/runtime/manager.py`：修改队列、attempt、取消、超时或停机时，必须检查正常、异常、超时、双重停机超时和立即重试路径。
- `src/teyvatnotes/runtime/executor.py`：新增任务类型时，同步修改 `TaskType`、请求 Schema、显式转换、执行器、README 和测试；只允许注册的确定性任务。
- `src/teyvatnotes/worker/`：修改领取、租约、错误分类、退避或恢复时，同步覆盖并发领取、最大尝试、永久失败、过期写入和崩溃恢复。
- `src/teyvatnotes/observability/`：修改关联上下文、日志白名单或指标时，必须覆盖作用域恢复、敏感数据脱敏、并发计数和快照隔离。
- `src/teyvatnotes/config.py` 与 `api/security.py`：修改环境变量、鉴权或限流时，必须覆盖缺失/非法配置、密钥隐藏、无凭据/错误凭据、配额恢复、并发计数和 OpenAPI 安全声明。
- `Dockerfile`、`compose.yaml`、`.env.example`：修改容器配置时，必须检查非 root、无真实密钥、持久卷、迁移依赖、健康检查、只读文件系统和停机宽限期。
- `.github/workflows/ci.yml`：修改 CI 时，必须检查只读权限、锁文件安装、质量门禁、PostgreSQL 健康检查、迁移和真实集成测试是否都实际执行。
- `scripts/`：演示与基线脚本必须可由测试导入，不访问外部网络，不硬编码密钥，不使用固定 sleep 证明并发正确性，并保持 mypy strict 通过。
- `docs/architecture.md`、`recovery-runbook.md`、`stage-one-acceptance.md`：架构与操作说明必须区分实际能力和待办边界；恢复步骤不得默认执行破坏性命令。
- `src/teyvatnotes/api/sse.py`：修改游标、心跳或关闭语义时，覆盖断线重连的无重复、无缺口与无遗留生成器。
- `src/teyvatnotes/api/`：修改公开契约时，同步更新 Schema、错误映射、OpenAPI 声明、API 测试和 README 示例。
- `worker_main.py` 与 `api/worker_monitoring.py`：循环意外结束必须结束监控服务器；停止时等待当前合作式执行完成，确保连接池释放。`tests/test_worker_monitoring.py` 验证共享指标、readiness 失败、空闲/执行中停止与循环故障。
- `tests/test_task.py`、`test_repository.py`、`test_api.py`、`test_runtime.py` 分别守护领域、存储、HTTP 和并发运行时边界；`test_worker.py` 守护领取、重试、租约和恢复；`test_observability.py` 守护 SSE、关联日志、健康检查和指标；`test_security.py` 与 `test_deployment.py` 守护安全、容器、CI 和文档契约；`test_stage_one_acceptance.py` 守护最终演示与并发基线；`test_database.py` 守护元数据与离线迁移。
- `uv.lock` 是自动生成的依赖锁文件，只说明用途和关键元数据，不手工编辑，也不做逐行讲解。

## 七、编码与测试规范

- 使用 Python 3.12 语法和完整类型标注；`mypy` 保持 strict 通过。公共类、函数和非直观并发分支应有简洁中文 docstring 或注释。
- 遵循 Ruff 配置：行宽 100，检查集合为 `B/E/F/I/SIM/UP`。不要用忽略规则掩盖可直接修复的问题。
- `TaskRepository` 端口为兼容异步数据库而统一使用 `async def`；内存实现中不得伪造 I/O 或加入无意义的调度。
- 优先写从公开边界观察行为的失败测试，再做最小实现和重构。并发测试优先使用 `Event`、门闩或可控 Fake Executor，避免依赖脆弱的固定 `sleep`。
- API 测试使用 `TestClient` 或 HTTPX `ASGITransport` 直接调用 ASGI 应用，不启动真实服务器、不访问真实网络；需要 Runtime 时必须进入 lifespan，并在退出后断言资源已清理。
- 测试至少覆盖成功、非法输入、非法状态转换、内部异常、并发竞争、取消、超时和停机路径；修复缺陷时先添加能复现问题的回归测试。
- 标准检查命令：

```powershell
uv sync --locked
uv run pytest -q
uv run ruff check .
uv run ruff format --check .
uv run mypy
```

- 只改文档时可按风险缩小验证范围，但至少检查 Markdown 内容、路径、命令和当前实现是否一致。修改 Python、配置或依赖后，应运行全部四项质量检查。

## 八、阶段演进边界

- 第 4 周：PostgreSQL、SQL、SQLAlchemy 2.x 异步接口、Alembic、事务、原子领取、行锁、租约字段、唯一约束和索引；目标表为 `tasks`、`task_attempts`、`task_events`。
- 第 5 周：API 与 Worker 的 Python 边界、最大重试、指数退避/抖动、错误分类、租约恢复语义与至少一次执行；支撑这些能力的具体 SQL 仍归第四周。
- 第 6 周：`GET /v1/tasks/{task_id}/events`、SSE 断线续传、结构化日志、关联 ID、`/health/live` 和 `/health/ready`。
- 第 7 周：集中配置、Bearer API Key、进程内限流、Dockerfile、API/Worker Compose、持久卷、健康检查和非 root 容器。
- 第 8 周：GitHub Actions CI、真实 PostgreSQL 服务与迁移验收、七场景演示、无脆弱阈值的并发基线、架构/状态机/表关系图和故障恢复手册。
- 第四周补全：`tasks.available_at/attempts_in_cycle/request_id`、attempt 租约审计字段、`FOR UPDATE SKIP LOCKED` 原子领取、有效租约写入校验、过期恢复和独立 Worker 进程。
- 后续阶段的核心验收始终是：长时间运行、可能失败、重试、取消和中断的任务，在并发及服务重启后仍然可解释、可恢复、可测试。
- 未收到对应阶段的明确任务前，保持当前实现简单；不得提前引入 Redis、Celery、Kafka、Kubernetes、DDD、CQRS、Event Sourcing、复杂 OAuth 或完整可观测性平台。

## 九、完成定义与文档维护

- 完成代码修改前，先运行最相关测试，再运行完整的 pytest、Ruff 检查、格式检查和 mypy；报告实际结果，不能用“应该通过”代替验证。
- 检查最终 `git diff`，确保只包含任务范围内的变更；如果发现不属于自己的修改，保留并在交付时说明。
- 公开行为、启动方式、依赖、限制或目录结构变化时同步更新 README；只有内部重构且契约不变时可不更新。
- README 面向首次接触项目的外部读者，只展示理解、运行和使用当前项目所必需的内容，例如项目用途、已实现功能、快速开始、必要配置、使用示例与已知限制。
- README 不记录项目实现过程，包括方案讨论、开发步骤、迭代经过、调试试错和测试流水。实现变更影响外部使用时，只更新读者需要知道的现行用法、行为或限制。
- README 不得写入只面向作者的学习路线、按周进度、知识点讲解、个人复盘、待办提醒、用户与智能体的对话或授权经过，也不得追加本机环境状态、按日期的补验记录、单次测试通过数量和临时问题处理流水。
- 智能体协作约束放在 AGENTS.md；详细接口、部署、迁移和评测说明放在对应的 `docs/` 文档，README 只保留外部读者需要的摘要与相对链接。
- 清理 README 时保留影响使用的真实能力边界、已知问题、必要配置、数据风险及可复现命令；不因对外展示而夸大功能或隐藏限制。完成一次开发或测试不构成向 README 追加过程记录的理由。
- 用户已删除 `D:\code\Codex_function_teaching\agent_runtime_foundation_Code_Review与Python语法学习.md` 并取消后续维护；除非用户重新明确要求，否则不恢复、重建或更新该文档，也不再要求逐文件输出 Python 语法学习讲解。
- 每当 `D:1code1TeyvatNotes` 新增或更新一周的学习内容时，仍由子智能体承担 Code Review；审查结论随当次开发结果报告，不生成上述学习文档。代码质量检查和必要的项目使用文档维护要求不变。

## 十、第二阶段第一周边界

- 用户已确认以原神攻略 Agent 为第二阶段项目，并明确将模型 API 延后。本周只实现服务商无关的模型协议、离线资料导入与知识持久化，不接模型 SDK、不读取模型凭据、不调用付费 API。
- 第一周入口为 `python -m teyvatnotes.knowledge.cli`，输入是人工准备的 UTF-8 JSON 快照；当前不获取真实网页/字幕/图片，不执行 ASR、OCR、视觉理解或 RAG 检索。
- `sum_numbers/word_count`、任务状态机、TaskExecutor 和现有 HTTP 契约保持不变；知识 CLI 的步骤检查点不能被描述为 TaskWorker 已有的步骤恢复或租约副作用隔离。
- 快照、解析器版本和分块配置进入文档版本身份；旧版本及证据不可覆盖。自定义解析器必须显式指定版本，算法变化必须升级版本。
- 默认导入草稿；示例资料、未知版本、不完整内容及关键视觉证据缺失不能发布。用户在 2026-10-04 明确要求体验服和正式服不作准入区分：content_nature 保留来源标签，但不阻断发布、检索或 RAG 索引，也不单独触发拒答；精确版本、完整性及人工审核规则不变。发布检查只验证结构和声明，不替代事实审核。
- 此规则更新采用 offline-v2 处理身份，重新导入生成新版本，不覆盖 offline-v1 检查点；数据库约束由 0006 显式迁移同步，不自动改业务库。此更新当轮PG暂缓；用户在2026-10-05启动服务后已补验隔离库全套942通过，仍不表示原有测试库/业务库已迁移。
- `samples/guides/` 是长期、明确虚构的教学样本，不是一次性文件，不代表真实游戏机制或已验证来源。
- `tests/test_guide_postgres.py` 只创建随机隔离 schema，不删除现有任务表或测试证据；读取实际测试结果，不能把测试存在或离线 SQL 生成当成真实 PostgreSQL 通过。
- 开发与后续衔接以 `docs/guide-ingestion.md` 和 `docs/guide-answer-acceptance.md` 为规格；按周代码审查仍由子智能体完成，结论随当次开发结果报告。

## 十一、第二阶段后续检索边界

- `knowledge/query_cli.py` 提供关键词检索、按库内证据 ID 回读和问答准备，默认使用显式 `DATABASE_URL`；不接模型、不读模型凭据、不获取真实网站，不扩展 Task API、Worker 或 SSE。
- 正常范围只读非虚构 `published`；`--demo` 只在临时内存读取虚构 `draft`，两种范围不得混合，也不能在正常查询中增加任意草稿开关。
- 精确匹配版本/实体/类型；资料全部条件必须由玩家条件明确满足。同来源、版本和类型先选最新可见版本再做关键词/条件过滤，显式历史证据 ID 可继续按范围回读。
- 关键词是 lower 去重后的字面子串，分数不是模型置信度；SQL 参数化 strpos，不能将 `%/_` 或用户字符串拼接成 SQL。单次关键词最多 12 个、命中最多 10 个、每次上下文最多 20 个块。
- 回读是有界已存文本上下文，不能描述为完整父段落或全部任务步骤；截断必须显式标记并使问答准备降级。
- 问答准备结果为 needs_clarification、insufficient_evidence 或 evidence_ready，没有 answer 字段，model_called 始终为 false；不得将它当作建议文档中模型问答阶段已完成。
- 本阶段真实库新增测试仍创建并保留随机 schema；全套旧测试若需要重建表，只能在本任务专用可丢弃数据库执行，不能使用原配置库或业务库。
- 公开行为依据 `docs/knowledge-retrieval.md`；源码/配置修改仍需完整质量门禁。

## 十二、通用模型配置与单轮问答边界

- 用户最新要求彻底去除平台/模型硬编码及兼容入口。模型适配器为 `models/responses.py`，只读取 `LLM_API_KEY/BASE_URL/MODEL/TIMEOUT_SECONDS/STRUCTURED_OUTPUTS/API_STYLE`；平台地址、模型与密钥必须显式提供，无厂商默认值、旧变量别名或自动回退。当前协议仅 `responses`；支持新的协议须新增明确适配器，不能伪称任意平台都兼容。此前本机 OpenASI/Sol 选型是用户配置和历史验收，不是代码约束。第十、十一节关于无模型的限制仍适用于原入库/准备入口，不代表新独立回答入口。
- 本机凭据从已忽略的 `.env` 经 `uv run --env-file .env` 或部署环境注入，只使用 `LLM_API_KEY`。聊天中出现的密钥必须撤销/轮换，不能写入源码、示例、文档、日志或测试，也不执行真实付费验证直到用户确认新密钥配置。
- `knowledge/answer_cli.py` 正常模式只读业务 PostgreSQL 的正式资料，先检索/追问再延迟读取凭据；单次问答只生成一次，不自动重试、供应商回退或修复输出。`--demo` 是虚构内存资料+无网络模拟摘录，model_called=false，不能当作真实模型质量证明。
- 提示上下文不静默丢前置条件；严格 JSON、证据 ID 存在性和显式 required_* 持有子集由程序检查，引用链接从库内来源/位置派生。上述结构检查不是语义支持验证或游戏事实审核，不能宣称自然语言中遗漏的装备需求也已被程序识别。
- 默认提示词 JSON 模式；text.format 仅在显式 LLM_STRUCTURED_OUTPUTS=true 时发送，第三方兼容未经真实验证。未知用量/费用用 null，不能套用 OpenAI 官方价格；失败和取消可能已收费。
- HTTP 响应最多256 KiB，单调用默认60秒、问答用例检索+生成最多90秒，不代表CLI整个进程硬时限。CancelledError 合作传播，清理客户端，不承诺服务商停止计费。错误仅稳定代码；无重定向、无代理环境读取、无密钥回显。
- Task API、Worker、租约、SSE及确定性任务保持不变。私有会话已在独立入口实现，见第十五节；离线虚构流程评测见第十六节，不等于真实固定答案质量验收。流式回答和实际资料审核仍为后续内容。工具循环见第十三节。单轮公开行为依据 docs/model-answering.md；按周 Code Review 仍由子智能体完成，结论随当次开发结果报告。

## 十三、最小只读工具循环边界

- 学习建议13.11第3项通过 application/guide_agent.py、knowledge/agent_cli.py 和 models/responses_tools.py 手写实现，不引入框架/MCP，不改旧单轮入口或公开任务API/Worker。
- 仅提供 search_knowledge 和 read_evidence：范围由已确认玩家字段固定；模型不能更改版本/实体/条件，不能任意URL/文件/SQL/函数执行。回读ID须由本次会话实际检索授予；预检和搜索摘要不进入最终引用账本，截断/超预算回读不加入。
- 每次问答有独立历史/工具会话；最多4模型尝试、3工具尝试、每轮单工具。工具失败也计数，重复call_id拒绝；总用例90秒、单工具10秒、单请求UTF-8编码48k字节、工具JSON20k字符。20k已报告Token是事后软阈值，不是收费承诺；未知用量和费用仍为null。
- 正式原生Responses工具调用延迟读本机通用LLM凭据，不自动重试/回退；store=false且本次内存保留必要加密reasoning续接，不记录正文/reasoning/非法原始工具参数。私有CLI trace可包含验证后的玩家关键词，不能接到公开API或中央日志。
- --demo是固定离线search/read/摘录，model_called=false，不证明真实模型自主选择。第三方Responses原生工具/加密reasoning兼容尚未付费实测；旧Sol基础生成成功不能代替。公开规范见 docs/tool-agent.md，仍需独立事实/语义评测与真实审核样本。

## 十四、RAG 基础设施与离线检索边界

- 学习建议13.11第4项本轮完成可插拔索引/检索基础：domain/rag.py、application/rag_ports.py/rag_service.py、独立knowledge/rag_cli.py。正式语义Embedding尚未配置，不得把本地哈希特征、Sol Responses文字或索引表存在描述成真实语义能力。
- 用户要求保留本机现有回答平台/模型取值；通用LLM字段迁移不改变取值，不擅自更换模型、修改密钥/回答配置、下载训练权重或探测付费Embedding接口。DemoLexicalEmbedding只准fixture草稿，semantic=false；正式新CLI向量/混合显式embedding_not_configured，keyword无需Embedding。旧问答/工具默认检索不变。
- 候选最多500，先按来源选精确版本/类型下最新可见文档，再做实体/条件过滤，然后才排序；超预算不截前缀，新版缺索引不回退旧版。profile身份包含provider/model/revision/dimension/demo/semantic性质，章节+正文哈希绑定chunk；错维度/NaN/Inf/零向量/旧哈希拒绝。
- 全部批次验证后一次原子保存；相同(profile,chunk)同值幂等、不同值报vector_conflict，不能覆盖历史。PostgreSQL JSONB表通过0004新增，属于最多500候选的小规模精确余弦，不是pgvector/ANN；不自动迁移业务库。随机schema测试留存证据，无用户许可不删除。
- 混合用倒数排名融合，保留词面/余弦分和两路排名；得分不是置信度。RagKnowledgeReader每问题独立、显式注入，授权本实例已命中ID回读，保留有序前序/相邻上下文和截断，不保证游戏事实/所有步骤。
- 固定12题×3模式仅是虚构词面流程对比，指标分开记录Recall@k/MRR、负例/范围越界/上下文缺口；不能称为真实语义提升或50题答案验收。行为、预算和未完成项见docs/rag-retrieval.md。

## 十五、私有会话与玩家条件边界

- 学习建议13.11第5项通过 application/conversations.py、独立 knowledge/conversation_cli.py、内存/PG仓储和0005迁移实现。默认仍用既有关键词检索；不得宣称补齐正式语义Embedding、真实攻略质量、自由文本自动记忆或50题验收。
- 每轮question必填，省略字段继承；装备/角色列表和conditions映射一旦提供则整项替换，不追加/逐键合并。null库存未知，[]明确没有；conditions={}清空，null非法。任意conditions键与库存不自动语义协调，变更须显式提供。
- 版本/实体/类型一旦确认，不可更改或清空；范围变化必须新建会话。每轮只传当前结构化条件，新建工具会话、授权账本和模型历史；旧答案只作为私有历史保存，不回放到新轮提示词。原始工具参数/opaque reasoning不持久化或跨轮回放。历史保存每轮条件与受控答案，不把旧建议显示为pending轮当前建议。
- 先以修订号CAS领取再调用答案模型，完成再CAS；两次通常共+2。PG短事务行锁不跨网络；陈旧修订在提取前拒绝时零调用；提取期间竞争可能已消耗一次提取，最终CAS失败须保留消耗、不生成答案、不自动重试。最多20个领取轮次（含错误/取消/中断），补丁16KiB、回复65536UTF-8字节、历史2000000字节；生成等待100秒不含DB/CLI全流程，沿用内部工具预算而非收费硬上限。
- 取消原样传播，存储失败可留下pending；仅显式interrupt，不能自动重放失联付费请求。迟到结果修订围栏拒绝覆盖；中断不保证远端停止计算/收费，也不承诺exactly-once计费。关闭不删除，不允许pending或重新打开。
- GUIDE_SESSION_OWNER_ID必填UUID，是可信本机CLI的存储分区标识，不是密码/认证或公开多租户安全。明文JSONB含玩家条件、答案和引用，没有加密/保留期/删除接口；禁止接入公开Task读接口或中央日志。持有DB账号仍可绕过CLI直接读库。
- 正式只用业务DATABASE_URL，不回退TEST_DATABASE_URL/内存、不自动升级。JSON/规则create及show/close不读模型密钥；显式--extract-model的understand/create/ask会先读取通用LLM配置做一次提取，可能计费，create仍不生成答案；答案模型仍待证据就绪才调用。不改变本机模型/平台取值，不发付费请求。行为依据docs/conversations.md，保留旧验收历史。

## 十六、离线评测与可靠性边界

- 学习建议13.11第6项先实现独立knowledge/evaluation_cli.py的demo与reliability；该离线入口不读取.env、不连接数据库/网络、不调用付费API、不修改用户模型配置，不改变Task API/Worker/SSE或旧问答JSON。其开发当轮按用户要求暂缓数据库验证；2026-10-05用户启动服务后补验真实PG，但不能以此将离线虚构评测称为真实答案质量验收。
- 固定50题按15角色/15任务/10缺条件/5冲突/5范围外分组，development30/holdout20，共用四份虚构快照九块；不能称为50条真实独立问题或盲测质量。人工source_id+ordinal参考与规范化快照哈希固定，漂移/缺参考在生成前失败，禁止按检索排名反推标注或为全绿删除失败。当前45流程通过、5冲突失败须如实保留。
- 指标区分工具实际搜索、首搜MRR和成功回读覆盖；检查范围/引用存在/显式资源/标注步骤和section，不证明语义支持或真实机制。semantic_citation_support/game_fact_correctness保持null，manual_semantic_review_required始终true，publication_ready始终false。未知Token/费用null，真实API次数与模拟次数分开；不注入真实客户端却报告demo-only。
- 全报告含玩家输入/答案/受控参数，是私有调试资料，禁止接入公开读取或中央日志；仅稳定错误，不输出非法原参数、异常原文、opaque reasoning或凭据。仅输出stdout，不自动覆盖用户报告。
- application/answer_events.py为每次问答可选单次使用的进程内元数据记录；固定起止、最多4模型/3工具成对事件、无正文/参数/条件，正常最多16事件另设32硬上限。旧序列化契约不变，不称文本流、公开SSE、持久事件/重放。取消传播，不自动重发付费调用；异常元数据不得伪记成功。
- 九项可靠性探针仅临时内存模拟：检查点复用/调用与字节预算/已知与未知Token/超时/取消/事件，不等于PG恢复、TaskWorker步骤恢复或恰好一次收费。说明见docs/evaluation.md；真实语料、语义Embedding、付费兼容/质量、跨attempt成本、持久文本流仍待授权与环境。

## 十七、开发者专名词表与入库校对边界

- 用户确认本次误识别“奥萝塔”的标准角色名为“奥黛塔”。用户最新要求：name通过读取米游社原神Wiki对应角色/武器/圣遗物图鉴页面维护；aliases由开发者维护，或登记测试中发现并核实的误识别案例。2026-10-04.2词表包含本轮实际读到的129/252/63个分类标准名，每条留读取日和详情URL，不伪称逐页机制审核或永久完整；不得从转录、相似度或模型记忆冒充补齐游戏名录，不推导未确认昵称。刷新Wiki不能覆盖已有alias，缺项/改名不自动删除或模糊合并；不调用付费模型生成词表。
- 精确名称字段可以更正登记别名；正文匹配同时保护已登记长名称，邻接非ASCII字母的别名、跨类别歧义保持原文并阻断发布。近似候选仅供人工审核，最多256词形比较，超限不截取有偏候选。未登记正文名称并未自动全部识别；显式recognized_terms中未知项阻断，未登记主角色仅告警以支持渐进维护。
- 当前入库处理版本offline-v3；保存原正文/实体、更正位置、完整词表和哈希，词表内容变更必须产生新文档身份，不覆盖旧来源、证据、检查点或真实模型输出。解析及名称校对用asyncio.to_thread，不在事件循环中做整表候选比较。
- 词表检查CLI不连接网络/数据库、不写文件。import-snapshot的--terms显式选择词表；错误不能悄悄退回空表。JSON问题和JSON会话补丁不自动规范化，调用方仍使用标准名称；自然语言--text仅规范化已登记别名，已确认会话范围不得被隐式更改。详见docs/terminology.md。
- 2026-10-05用户启动PG后，验证必须使用全新专用数据库与随机schema：旧test_postgres_repository._migrate会downgrade，不能直接指向原agent_runtime_test或业务库。测试runner仅临时选隔离TEST_DATABASE_URL，剥离AGENT_RUNTIME_/LLM_/GUIDE_等本机运行配置，不改.env。2026-10-05隔离库历史验收942项全过（896非PG+46真实PG），新tests/test_terminology_postgres.py验证444词表跨Engine持久化/更正/原文/旧证据/检查点复用，详情见docs/postgres-wiki-acceptance.md。测试库和schema留存；业务库升级仍须明确核对与授权。

## 十八、自然语言输入与模型辅助候选提取

- 按建议13.14先交付独立QuestionUnderstandingService、字段来源、保守规则和现有conversation_cli的understand/create/ask --text；不新增平行会话服务、不改QuestionSchema或公开Task API。
- 只有当前明确文本和已确认QuestionRequest可作为条件来源，不能从旧模型答案推断持有。标准名/登记别名精确处理；相似候选、模糊指代、未知分句或范围切换均不得产生可保存补丁。
- 支持部分明示条件的追问保存必须用现有ask的clarification分支，跳过全部检索/答案生成，已发生的模型提取仍须计数；歧义整轮不保存。省略/未知null/明确没有[]语义不得混淆，库存删减要生成完整列表；已有conditions时库存更新须转JSON显式协调。
- understand原位改为async，所有调用方使用await；不保留同步兼容包装。默认规则零提取；只有显式--extract-model才复用通用ModelClient/LLM_*，无规则回退、自动重试或厂商默认值。
- 模型仅辅助分句/候选结构，不是任意自然语言理解。线协议status+segments(start/end/quote/updates列表)，updates仅闭合的9种field/value；严格拒绝重复JSON键/字段、额外属性、非法类型及超16KiB响应。每段须逐字匹配原文并与同一本地校验器完整结构一致；顺序不重叠且非分隔文字全覆盖，再做整轮冲突/范围校验。
- 提取完整上下文（system/user/schema及封套预留）UTF-8最多48k，读取凭据前检查；只发送当前原话与已确认结构，不回放旧question或答案。远端strict模式仍未经第三方真实验证。
- 单个CLI请求的GenerationLedger在提取和工具回答间共享4次尝试、90秒生成窗口、20000已报告Token软阈值；提取最多1次且最多30秒，提取后最多剩3次回答尝试。最后一次模型调用请求工具时不执行无机会回送的工具；数据库/全进程不是硬截止，费用未知保持null。
- 每阶段尝试/用量/耗时与整轮round_usage分开，追问/错误/取消以及提取后CAS或清理失败不能误报零调用。ask历史回复顶层统计为整轮，子阶段另存；understand和create的提取统计只在当次输出，不是持久费用账本。取消原样传播，UnderstandingCancelled.result保留提取统计；不修改凭据或擅自做付费验证。
- 13.14理解合同回归复用evaluation_cli的understanding命令，all/rules/simulated仅规则或人工固定候选，无真实模型/数据库/检索/回答。31场景对应29种原话+上下文，双模式62次执行不能冒充62道独立题；正确追问和拒绝也算合同通过，不是理解准确率。候选参考独立人工标注，执行前验证固定hash；不得用被测解析器反推参考或静默接受漂移。来源只验证基本一致性与可定位性，不证明语义支持；真实质量和未知费用保持null，报告私有且仅stdout，故障保留失败行，取消继续传播。说明见docs/evaluation.md。

</INSTRUCTIONS>
