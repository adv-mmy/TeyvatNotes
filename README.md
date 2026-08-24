# Agent Runtime Foundation

这是一个用于学习可靠 Agent Runtime 基础能力的 Python 项目。项目会先使用确定性任务验证状态、失败、重试、取消和测试，再逐步接入 FastAPI、PostgreSQL、独立 Worker 与 SSE。

## 当前阶段

第 8 周：GitHub Actions CI、真实 PostgreSQL 集成测试、七场景验收演示、并发基线、架构图和故障恢复手册。

第四周的 SQL、事务、索引、SQLAlchemy 与 Alembic 内容保留在独立章节。当前未在本机运行 PostgreSQL；本周不新增表、迁移或 Worker 原子领取 SQL。Compose 暂时让 API 与内嵌 Worker 同进程运行，待 PostgreSQL Worker SQL 完成后再拆成两个进程。

当前实现包含：

- `TaskStatus`：任务状态枚举；
- `TaskResult`：任务执行结果；
- `Task`：任务实体及状态转换；
- `InvalidTaskTransition`：非法转换异常；
- 合法、非法和终态转换的参数化测试。
- `TaskRepository` 协议和线程安全的内存实现；
- 创建、查询、列表、取消和重试 API；
- 分页、状态过滤、Repository 依赖注入；
- 统一的 `code/message/details/request_id` 错误响应；
- 基于 `asyncio.Queue` 的固定 Worker 任务队列；
- 使用 `asyncio.Semaphore` 限制真正执行任务的并发数；
- 任务级超时、运行中取消和重试后重新入队；
- FastAPI lifespan 启停运行时，关闭时先等待任务，超时后显式取消；
- `sum_numbers` 和 `word_count` 的异步确定性执行器。
- `tasks`、`task_attempts`、`task_events` 三张 PostgreSQL 表；
- SQLAlchemy 2.x `AsyncEngine` / `AsyncSession` 和可配置连接池；
- PostgreSQL Repository，在同一事务中保存状态、结果、attempt 和 event；
- Alembic 首个迁移，可从空数据库重建表、约束和索引；
- `Idempotency-Key` 创建幂等性与数据库唯一约束；
- 面向常用列表查询的联合索引及 JSON `EXPLAIN` 命令。
- `TaskCommandHandler` 抽象，使 API 可选择内嵌执行或只持久化命令；
- `WorkerRepository` 端口和可运行的内存原子领取/租约实现；
- 保存每次 attempt 的 Worker、时间、结果、错误和下次重试时间；
- 最大尝试次数、指数退避、有上限抖动和可注入时钟/随机源；
- 可重试、永久错误和未预期错误分类；
- 过期租约恢复、旧 Worker 防过期写入和无活动 attempt 取消收敛。
- `task_created`、`status_changed` 和 `task_progress` 事件，以递增 ID 提供 SSE 断点续传；
- 通过 `request_id`、`task_id`、`attempt_id` 和 `worker_id` 串联 API 与 Worker 日志；
- 单行 JSON 结构化日志，不记录请求体、查询参数、异常原文或原始 URL；
- `/health/live`、`/health/ready` 和进程内 `/metrics` 快照。
- 从环境变量集中加载并校验密钥、数据库、Runtime 和限流配置；
- `Authorization: Bearer <API Key>` 保护创建、取消和重试；
- 创建与重试共享线程安全的进程内滑动窗口限流；
- 非 root、只读文件系统的应用镜像，以及 PostgreSQL 持久卷、迁移、健康检查和优雅退出 Compose 配置。
- GitHub Actions 自动运行 Ruff、格式、mypy、单测、并发基线、Alembic 迁移和真实 PostgreSQL 集成测试；
- 可重复的阶段一七场景演示与进程内并发基线；
- Mermaid 架构图、状态机图、表关系图，以及不删除数据的故障恢复手册。

领域层仍不依赖 FastAPI、数据库或 LLM SDK。

## HTTP API

| 方法 | 路径 | 用途 |
| --- | --- | --- |
| `POST` | `/v1/tasks` | Bearer 鉴权并限流；创建任务，可携带 `Idempotency-Key` |
| `GET` | `/v1/tasks/{task_id}` | 查询任务 |
| `GET` | `/v1/tasks/{task_id}/events` | 订阅 SSE 事件，支持 `Last-Event-ID` |
| `GET` | `/v1/tasks?status=queued&offset=0&limit=20` | 分页列表和状态过滤 |
| `POST` | `/v1/tasks/{task_id}/cancel` | Bearer 鉴权；请求取消任务 |
| `POST` | `/v1/tasks/{task_id}/retry` | Bearer 鉴权并限流；重试失败任务 |
| `GET` | `/health/live` | 检查进程存活 |
| `GET` | `/health/ready` | 检查存储和 Runtime 是否就绪 |
| `GET` | `/metrics` | 读取当前进程的 attempt 指标 |

本地启动（默认 `embedded` 模式，保留第三周的进程内 Runtime）：

```powershell
$env:AGENT_RUNTIME_API_KEY = python -c "import secrets; print(secrets.token_urlsafe(32))"
uv run fastapi dev main.py
```

启动后可访问 `http://127.0.0.1:8000/docs` 查看 OpenAPI 交互文档。

纯 API 进程入口：

```powershell
uv run fastapi dev api_main.py
```

`api_main.py` 使用 `api_only` 模式，只保存创建、取消和手动重试命令，不启动 `TaskRuntime`。未设置 PostgreSQL Worker 适配器时，不要把它当作已完成的双进程部署：新任务会保持 `queued`。

当前允许的任务类型：

- `sum_numbers`：`payload.numbers` 包含 1～100 个整数，每个整数在 `-1000000～1000000` 之间；
- `word_count`：`payload.text` 是 1～10000 个字符的非空白文本。

### 调用示例

创建任务：

```powershell
curl.exe -i -X POST http://127.0.0.1:8000/v1/tasks `
  -H "Authorization: Bearer $env:AGENT_RUNTIME_API_KEY" `
  -H "Content-Type: application/json" `
  -H "Idempotency-Key: demo-sum-001" `
  -d '{"task_type":"sum_numbers","payload":{"numbers":[1,2,3]}}'
```

成功时返回 `201 Created`，并在 `Location` 响应头中给出查询路径。
创建接口只完成持久化和入队，响应中的初始状态为 `queued`；Worker 会在后台把任务推进到 `running`，最终变为 `succeeded`、`failed` 或 `cancelled`。
对同一个非空 `Idempotency-Key` 重复发起创建请求时，系统返回第一次创建的任务，不会再次入队。

查询单个任务：

```powershell
curl.exe http://127.0.0.1:8000/v1/tasks/<task_id>
```

列出排队中的任务：

```powershell
curl.exe "http://127.0.0.1:8000/v1/tasks?status=queued&offset=0&limit=20"
```

请求取消任务：

```powershell
curl.exe -X POST http://127.0.0.1:8000/v1/tasks/<task_id>/cancel `
  -H "Authorization: Bearer $env:AGENT_RUNTIME_API_KEY"
```

重试失败任务：

```powershell
curl.exe -X POST http://127.0.0.1:8000/v1/tasks/<task_id>/retry `
  -H "Authorization: Bearer $env:AGENT_RUNTIME_API_KEY"
```

订阅任务事件：

```powershell
curl.exe -N http://127.0.0.1:8000/v1/tasks/<task_id>/events
```

断线前最后已确认的 SSE `id` 为 `3` 时，重连只读取更新事件：

```powershell
curl.exe -N http://127.0.0.1:8000/v1/tasks/<task_id>/events `
  -H "Last-Event-ID: 3"
```

错误响应示例：

```json
{
  "code": "task_not_found",
  "message": "任务不存在",
  "details": {"task_id": "..."},
  "request_id": "..."
}
```

| HTTP 状态码 | 错误代码 | 含义 |
| --- | --- | --- |
| `401` | `authentication_required` | 缺少 Bearer API Key 或凭据不匹配 |
| `404` | `task_not_found` | 指定任务不存在 |
| `409` | `invalid_task_transition` | 当前状态不允许取消或重试 |
| `429` | `rate_limit_exceeded` | 创建或重试超过当前滑动窗口配额 |
| `422` | `validation_error` | 请求体或查询参数不符合 Schema |
| `500` | `internal_error` | 未预期的服务端错误 |
| `503` | `runtime_unavailable` | 运行时尚未启动或正在停止，不接收新任务 |

## 分层与数据边界

```text
HTTP / FastAPI
  → Pydantic Schema
  → TaskService
  → Task 状态机
  → TaskRepository
```

路由只负责输入转换、调用用例和输出转换。取消与重试的合法性由第一周实现的 `Task.transition_to()` 保证，避免 API 层出现第二套状态规则。

设置 `DATABASE_URL` 时使用 PostgreSQL Repository；未设置时保留内存 Repository，方便无数据库的本地练习和单元测试。

## PostgreSQL 持久化

### 建表与迁移

使用专用 PostgreSQL 数据库，然后先执行迁移：

```powershell
$env:DATABASE_URL = "postgresql+asyncpg://postgres:postgres@127.0.0.1:5432/agent_runtime"
$env:AGENT_RUNTIME_API_KEY = python -c "import secrets; print(secrets.token_urlsafe(32))"
uv run alembic upgrade head
uv run fastapi dev main.py
```

应用不会在启动时自动改表，避免多个应用实例并发执行迁移。启动前应由部署步骤明确运行 `alembic upgrade head`。

| 表 | 主要用途 |
| --- | --- |
| `tasks` | 任务类型、参数、当前状态、结果/错误、幂等键、版本号和时间 |
| `task_attempts` | 每次执行的编号、开始/结束时间及错误 |
| `task_events` | 创建、状态和 0/100 进度事件，作为 SSE 数据源 |

`PostgresTaskRepository.transition()` 用 `SELECT ... FOR UPDATE` 锁定任务行。领取、领域状态校验、`tasks` 更新、`task_attempts` 更新和 `task_events` 插入在同一个事务中提交；任一 SQL 失败都会整体回滚。
列表查询在短生命周期 `REPEATABLE READ` 事务中先计算 `total` 再取分页数据，使两条 SQL 共享同一 MVCC 快照，避免并发插入导致单次响应的总数与列表不属于同一时点。

### 约束与索引

- `uq_tasks_idempotency_key` 由 PostgreSQL 保证非空幂等键唯一，Repository 使用 `INSERT ... ON CONFLICT DO NOTHING` 处理并发重复请求。
- `ix_tasks_created_at_id` 支持不过滤状态的稳定分页排序。
- `ix_tasks_status_created_at_id` 支持按状态过滤后再按 `created_at, id` 排序。
- CHECK、FOREIGN KEY、UNIQUE 和非空约束作为应用校验之后的最后一层不变量保护。

对主要列表查询执行 JSON `EXPLAIN`：

```powershell
uv run python -m agent_runtime_foundation.database.explain
```

数据量很小时 PostgreSQL 可能正确地选择顺序扫描，因此不应只凭“是否出现 Index Scan”判断索引设计；还要结合数据分布、行数估算和实际耗时。

### PostgreSQL 集成测试

默认测试不需要数据库。如需验收空库迁移、持久化、事务回滚、并发幂等和 `EXPLAIN`，设置一个可丢弃的专用测试库：

```powershell
$env:TEST_DATABASE_URL = "postgresql+asyncpg://postgres:postgres@127.0.0.1:5432/agent_runtime_test"
uv run pytest -m postgres -q
```

> 集成测试开始时会把该数据库中本项目的 Alembic 版本先降到 `base`，再升级到 `head`。不要把 `TEST_DATABASE_URL` 指向开发或生产数据库。

## 第五周 Worker 与故障恢复

```text
API-only 进程
  → TaskCommandHandler 只持久化 queued 任务

TaskWorker（独立进程主循环的 Python 部分）
  → recover_expired()
  → claim_next()：queued → running，创建 attempt 和租约
  → TaskExecutor
  → complete_claim() / fail_claim()
  → 可重试时计算 retry_at，到期后重新领取
```

### 重试语义

- `PermanentTaskError` 和现有 `TaskExecutionError` 属于永久错误，第一次失败后不自动重试。异常原文不写入公开任务结果，API 只返回稳定错误信息。
- `RetryableTaskError`、执行超时和未预期异常属于可重试错误，但不得超过 `max_attempts`。
- `max_attempts` 限制单轮自动执行预算；用户显式调用手动重试会开启新一轮预算，同时保留全局递增的 `attempt_number` 用于审计。
- 第 `n` 次失败的基础等待为 `base_delay * 2 ** (n - 1)`，再受 `max_delay` 与 `jitter_ratio` 限制。
- 时钟和随机源可注入，测试不依赖真实 `sleep` 或不稳定随机数。
- 每次领取产生新 `attempt_id`，并保存 `running`、`retry_scheduled`、`retries_exhausted`、`permanent_failure`、`succeeded`、`lease_expired` 或 `cancelled` 结果。

### 租约与至少一次执行

Worker 领取任务后只拥有限时租约。如果 Worker 在执行完成后、保存成功结果前崩溃，新 Worker 会在租约过期后重新领取该任务。因此同一任务的业务副作用可能已经发生，却再次执行。

Worker 执行协程被强制取消时不会伪造持久化完成，也不会增加 completed/cancelled 指标；它记录 `task_abandoned` 日志并保留 running attempt，等待租约过期后由替代 Worker 恢复。

这是“至少一次”而不是“恰好一次”。通常无法只靠任务队列承诺恰好一次，因为执行业务副作用和记录成功不能对任意外部系统构成同一原子事务。执行器仍应使用业务幂等键、去重记录或可安全重放的操作。

### 当前边界

本轮已完成 `TaskWorker`、`WorkerRepository` 端口和内存验收实现，但还不是可跨进程共享的生产队列。待本机安装 PostgreSQL 后，将按第四周 SQL 范畴补充和验收 PostgreSQL 原子领取、租约持久化与跨进程恢复；这些不计入本次第五周实现。

## 第六周 SSE 与可观测性

### 事件与断点续传

`task_events.id` 是严格递增的事件游标。SSE 消息同时写入 `id`、`event` 和 JSON `data`；客户端重连时把最后已确认 ID 放入 `Last-Event-ID`，服务端只返回 `id > Last-Event-ID` 的事件。这避免重复已确认事件，也不跳过中间事件。

当前确定性任务只报告 0% 和 100% 进度。无新事件时发送 SSE 注释心跳；任务进入 `succeeded`、`failed` 或 `cancelled` 后，当前连接在排空已持久化事件后关闭。

### 关联 ID 与结构化日志

- `request_id` 标识一次 HTTP 请求，由服务端生成；客户端传入的 `X-Request-ID` 不会原样进入日志或事件；
- `task_id` 标识业务任务，`attempt_id` 标识一次执行，`worker_id` 标识执行者；
- 日志只记录白名单字段和异常类型，不记录 payload、查询参数、异常原文或未匹配原始 URL。

`main.py` 和 `api_main.py` 会为 `agent_runtime_foundation` logger 安装单行 JSON formatter。一条典型执行日志同时含有四个关联 ID，可从 API 请求追踪到 Worker attempt。

### 健康检查与指标

- `/health/live` 只表示 HTTP 进程存活；
- `/health/ready` 在 1 秒超时内检查存储；PostgreSQL 模式会验证 `tasks`、`task_attempts`、`task_events` 三张既有表可访问，存储失联、表未迁移或内嵌 Runtime 不接收任务时返回 `503/not_ready`；
- `/metrics` 返回 attempt 的开始/完成数、成功率、耗时总和/平均/最大值以及失败类型计数。

这里的 Metric 是数值聚合，Log 是离散事件记录，Trace 则是一次请求跨组件的因果链。本项目当前通过关联 ID 提供轻量追踪，尚未接入分布式 Trace 后端。`/metrics` 也是单进程、内存中的瞬时快照，重启会清零，API-only 与独立 Worker 之间不会自动聚合。

## 第七周安全、配置与 Docker Compose

### 配置与密钥

应用启动时由 `AppSettings.from_env()` 一次性读取配置。`AGENT_RUNTIME_API_KEY` 是必填项，必须是 32～256 位 Bearer 安全字符；密钥和 `DATABASE_URL` 使用 `SecretStr` 保存，并从配置对象的 `repr()` 中隐藏。真实 `.env` 已加入 `.gitignore`，仓库只提供不含真实凭据的 `.env.example`。

| 环境变量 | 默认值 | 用途 |
| --- | --- | --- |
| `AGENT_RUNTIME_API_KEY` | 无，必填 | 创建、取消和重试接口的 Bearer API Key |
| `DATABASE_URL` | 无 | PostgreSQL 异步连接串；未设置时使用内存仓储 |
| `AGENT_RUNTIME_EXECUTION_MODE` | `embedded` | `embedded` 或 `api_only` |
| `AGENT_RUNTIME_WORKER_COUNT` | `4` | 内嵌 Worker 协程数 |
| `AGENT_RUNTIME_MAX_CONCURRENCY` | `2` | 执行器并发上限 |
| `AGENT_RUNTIME_TASK_TIMEOUT_SECONDS` | `30` | 单任务超时 |
| `AGENT_RUNTIME_SHUTDOWN_TIMEOUT_SECONDS` | `5` | Runtime 停机宽限期 |
| `AGENT_RUNTIME_RATE_LIMIT_REQUESTS` | `30` | 一个滑动窗口允许的创建/重试总数 |
| `AGENT_RUNTIME_RATE_LIMIT_WINDOW_SECONDS` | `60` | 滑动窗口秒数 |

Bearer 鉴权使用恒定时间比较，凭据不会进入响应、事件或日志。当前只有一个配置主体，因此创建与重试共享同一配额；限流器只在单进程内生效，重启会清零，也不会在多个容器间自动共享。

任务类型仍由 `TaskType` 与请求 Schema 白名单限定，额外字段被拒绝。客户端输入不能指定 Python 函数、模块或系统命令。

### Compose 启动

先创建本机私有配置；`POSTGRES_PASSWORD` 应使用 URL-safe 值，因为它也会进入异步数据库 URL：

```powershell
Copy-Item .env.example .env
python -c "import secrets; print(secrets.token_urlsafe(32))"
```

把两次生成的随机值分别填入 `.env` 的 `AGENT_RUNTIME_API_KEY` 和 `POSTGRES_PASSWORD`，然后在安装 Docker Desktop 的新机器上用一条命令构建并启动：

```powershell
docker compose up --build -d
```

Compose 的启动顺序为：PostgreSQL 健康检查通过 → `migrate` 执行 `alembic upgrade head` → API readiness 通过。PostgreSQL 数据保存在命名卷 `postgres-data` 中，普通 `docker compose down` 后再次启动仍会恢复。应用镜像和 Compose 都固定使用 UID/GID `10001`，应用文件系统只读，仅挂载临时 `/tmp`，并用 `SIGTERM`、FastAPI lifespan 和 15 秒容器宽限期完成优雅退出。

当前 `api` 容器运行 `main.py`，其中同时包含 API 与内嵌 Worker。这是为了遵守“PostgreSQL Worker 原子领取 SQL 待数据库环境就绪后再实现”的现有边界。它能完成单容器创建、执行与持久化演示，但强制终止后自动领取遗留 `queued/running` 任务仍未完成；不得把它描述为已经实现独立 Worker 的生产部署。

## 第八周 CI、故障演练与阶段验收

### 自动化 CI

`.github/workflows/ci.yml` 在 push、pull request 和手动触发时运行两个 Job：

- `quality`：按 `uv.lock` 安装依赖，执行 Ruff、格式检查、mypy、非 PostgreSQL 测试和并发基线；
- `postgres`：启动带健康检查的 PostgreSQL 17，执行 Alembic 迁移，再运行所有 `postgres` 标记的真实 Repository 集成测试。

CI 中的测试数据库凭据仅属于隔离服务容器，不是部署密钥。测试失败不能用 `continue-on-error` 或 `|| true` 绕过；真实数据库测试也不能以 skip 代替通过。

### 七场景演示

以下命令会运行正常完成、幂等创建、运行中取消、超时自动重试、Worker 强制中断、替代 Worker 恢复，以及 SSE/日志关联七个场景：

```powershell
uv run python -m scripts.stage_one_demo
```

演示使用运行时随机生成的 API Key，不把凭据写入代码或日志。Worker 中断恢复使用现有内存租约模型和可控时钟，报告中的 `lease_expired -> succeeded` 证明 Python 策略；它不代表 PostgreSQL 跨进程 Worker SQL 已经完成。

完整步骤和期望结果见 [`docs/stage-one-acceptance.md`](docs/stage-one-acceptance.md)。架构、状态机和表关系见 [`docs/architecture.md`](docs/architecture.md)，故障处理见 [`docs/recovery-runbook.md`](docs/recovery-runbook.md)。

### 并发基线

```powershell
uv run python -m scripts.concurrency_baseline --tasks 200 --concurrency 20
```

脚本在真实 `await` 调度点记录并发峰值，验证 Semaphore 上限和所有任务正确收敛，再输出吞吐观测值。CI 不设置固定吞吐门槛，避免共享 Runner 性能抖动制造假失败；比较版本时应在同一环境多轮运行。当前结果只代表进程内内存实现，不代表 PostgreSQL 或生产容量。

## 异步运行时

```text
POST 创建/重试
  → TaskRuntime 原子检查 accepting 并写入 asyncio.Queue
  → 固定数量 Worker 从 Queue 取任务
  → Semaphore 控制进入执行器的并发上限
  → asyncio.timeout 限制单任务执行时间
  → Repository 原子保存状态与结果
```

默认配置：

| 配置 | 默认值 | 含义 |
| --- | --- | --- |
| `worker_count` | `4` | 固定 Worker 协程数量 |
| `max_concurrency` | `2` | 同时进入任务执行器的最大数量 |
| `task_timeout_seconds` | `30` | 单个任务执行超时 |
| `shutdown_timeout_seconds` | `5` | 关闭服务时等待已有任务的宽限期 |

`worker_count` 和 `max_concurrency` 刻意分开：Worker 可以提前从队列领取任务，但只有取得 Semaphore 的任务才能真正执行。

### 取消、超时和停机语义

- 取消 queued/running 任务时先记录 `cancel_requested`；若有执行协程，则调用其 `cancel()`，清理结束后落为 `cancelled`。
- 超时由 `asyncio.timeout()` 触发，任务落为 `failed`，错误为“任务执行超时”。
- 服务停止时先拒绝新的创建和重试，然后等待 Queue 的 unfinished task 归零。
- 超过停机宽限期仍未完成的任务会收到取消请求并明确落为 `cancelled`，不会从内存中无声消失。
- 执行器必须保持异步代码的合作式取消语义：清理完成后继续传播 `asyncio.CancelledError`。

### `await func()` 与 `create_task(func())`

```python
result = await execute(task)
```

当前协程会等待 `execute()` 完成后才继续，适合“后一步依赖前一步结果”的顺序流程。

```python
execution = asyncio.create_task(execute(task))
```

`create_task()` 会把协程调度成独立的 `asyncio.Task` 并立即把控制权还给调用者。运行时用它启动长期存活的 Worker 和每次任务执行，因此 HTTP 创建请求不必等待任务完成。创建后的 Task 必须保存引用、等待结果并在停机时清理；只创建却不管理会产生遗留协程或丢失异常。

## 状态机

```text
queued → running → succeeded

queued/running → cancel_requested → cancelled

running → failed → queued
```

允许的状态转换：

| 当前状态 | 目标状态 |
| --- | --- |
| `queued` | `running`、`cancel_requested` |
| `running` | `succeeded`、`failed`、`cancel_requested` |
| `cancel_requested` | `cancelled` |
| `failed` | `queued` |
| `succeeded` | 无，属于终态 |
| `cancelled` | 无，属于终态 |

## 环境要求

- Python 3.12 或更高版本；
- uv；
- PostgreSQL（只有持久化运行和 `postgres` 集成测试需要）。
- Docker Desktop（只有 Compose 部署需要）。

## 安装与检查

```powershell
uv sync --locked
uv run pytest -q
uv run ruff check .
uv run ruff format --check .
uv run mypy
uv run python -m scripts.stage_one_demo --no-logs
uv run python -m scripts.concurrency_baseline --tasks 200 --concurrency 20
```

## 项目结构

```text
src/agent_runtime_foundation/
├─ __init__.py
├─ config.py
├─ application/
│  ├─ models.py
│  ├─ events.py
│  ├─ task_commands.py
│  └─ task_service.py
├─ api/
│  ├─ app.py
│  ├─ error_handlers.py
│  ├─ security.py
│  ├─ sse.py
│  ├─ schemas.py
│  └─ routes/
│     ├─ health.py
│     └─ tasks.py
├─ database/
│  ├─ models.py
│  ├─ session.py
│  └─ explain.py
├─ domain/
│  ├─ errors.py
│  └─ task.py
├─ observability/
│  ├─ context.py
│  ├─ logging.py
│  └─ metrics.py
├─ runtime/
│  ├─ errors.py
│  ├─ executor.py
│  └─ manager.py
├─ repositories/
   ├─ base.py
   ├─ memory.py
   └─ postgres.py
└─ worker/
   ├─ errors.py
   ├─ models.py
   ├─ ports.py
   ├─ retry.py
   └─ service.py

migrations/
├─ env.py
└─ versions/20260810_0001_create_task_tables.py

tests/
├─ test_api.py
├─ test_database.py
├─ test_deployment.py
├─ test_observability.py
├─ test_postgres_repository.py
├─ test_repository.py
├─ test_runtime.py
├─ test_security.py
├─ test_stage_one_acceptance.py
├─ test_task.py
└─ test_worker.py

scripts/
├─ concurrency_baseline.py
└─ stage_one_demo.py

docs/
├─ architecture.md
├─ recovery-runbook.md
└─ stage-one-acceptance.md

.github/workflows/ci.yml

Dockerfile
compose.yaml
.env.example
```
