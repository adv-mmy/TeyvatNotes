# AGENTS.md

<INSTRUCTIONS>

## 一、通用约束

- 回答、代码注释和输出文件尽可能使用简体中文；中文文件统一使用 UTF-8 编码。
- 即使具备完全访问权限，删除任何文件前也必须先获得用户批准。
- 修改前先检查 `git status` 和相关文件，保留用户已有变更；不得用重置、覆盖或清理工作区的方式处理无关改动。
- 未经用户明确要求，不执行 Git 提交、推送、变基、打标签或创建 PR。
- 只做当前任务需要的修改。不要顺手升级依赖、重排无关代码、引入新基础设施或提前实现后续周内容。
- `D:\code\Codex_function_teaching\Agent开发技术栈学习路径建议.md` 是阶段目标与学习顺序的依据；实际代码、测试和 `pyproject.toml` 是当前行为的事实来源。两者不一致时，必须区分“已实现”和“规划中”，不得把规划描述成现状。

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

- `src/agent_runtime_foundation/domain/task.py`：修改状态、转换或结果语义时，同步更新领域、Repository、API 和 Runtime 相关测试。
- `src/agent_runtime_foundation/application/`：新增用例先扩展应用模型/端口，再由 API 或 Runtime 调用；不要让路由越过应用层操作仓储。
- `src/agent_runtime_foundation/repositories/memory.py`：修改并发或复制策略时，必须覆盖重复 ID、对象隔离、原子转换和并发竞争。
- `src/agent_runtime_foundation/repositories/postgres.py`：修改写路径时必须检查事务原子性、行锁、attempt/event 一致性、幂等冲突和稳定分页。
- `src/agent_runtime_foundation/database/` 与 `migrations/`：ORM 模型、迁移、约束和索引要保持一致；不在应用启动时自动改表。
- `src/agent_runtime_foundation/runtime/manager.py`：修改队列、attempt、取消、超时或停机时，必须检查正常、异常、超时、双重停机超时和立即重试路径。
- `src/agent_runtime_foundation/runtime/executor.py`：新增任务类型时，同步修改 `TaskType`、请求 Schema、显式转换、执行器、README 和测试；只允许注册的确定性任务。
- `src/agent_runtime_foundation/worker/`：修改领取、租约、错误分类、退避或恢复时，同步覆盖并发领取、最大尝试、永久失败、过期写入和崩溃恢复。
- `src/agent_runtime_foundation/observability/`：修改关联上下文、日志白名单或指标时，必须覆盖作用域恢复、敏感数据脱敏、并发计数和快照隔离。
- `src/agent_runtime_foundation/config.py` 与 `api/security.py`：修改环境变量、鉴权或限流时，必须覆盖缺失/非法配置、密钥隐藏、无凭据/错误凭据、配额恢复、并发计数和 OpenAPI 安全声明。
- `Dockerfile`、`compose.yaml`、`.env.example`：修改容器配置时，必须检查非 root、无真实密钥、持久卷、迁移依赖、健康检查、只读文件系统和停机宽限期。
- `.github/workflows/ci.yml`：修改 CI 时，必须检查只读权限、锁文件安装、质量门禁、PostgreSQL 健康检查、迁移和真实集成测试是否都实际执行。
- `scripts/`：演示与基线脚本必须可由测试导入，不访问外部网络，不硬编码密钥，不使用固定 sleep 证明并发正确性，并保持 mypy strict 通过。
- `docs/architecture.md`、`recovery-runbook.md`、`stage-one-acceptance.md`：架构与操作说明必须区分实际能力和待办边界；恢复步骤不得默认执行破坏性命令。
- `src/agent_runtime_foundation/api/sse.py`：修改游标、心跳或关闭语义时，覆盖断线重连的无重复、无缺口与无遗留生成器。
- `src/agent_runtime_foundation/api/`：修改公开契约时，同步更新 Schema、错误映射、OpenAPI 声明、API 测试和 README 示例。
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
- 每当 `D:\code\agent_runtime_foundation` 新增或更新一周的学习内容时，必须由子智能体承担 Code Review，并同步更新 `D:\code\Codex_function_teaching\agent_runtime_foundation_Code_Review与Python语法学习.md`。
- Code Review 文档必须以实际代码为依据，逐文件说明新增或变化的类、函数、方法、fixture、测试函数、关键代码块、调用关系和 Python/框架语法，并更新架构、风险、测试基线、维护清单和更新记录。
- 维护学习文档时，不得因此修改本项目 README、源码、配置或测试；只有用户另行明确要求时才可修改这些文件。
- 未经用户明确要求，不要为学习文档或 Code Review 的变更执行 Git 提交或推送。

</INSTRUCTIONS>
