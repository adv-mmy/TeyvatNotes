# 阶段一架构、状态机与表关系

本页描述 `0.8.1` 的实际实现：Compose 中 API 与独立 Worker 通过 PostgreSQL 协作；`main.py` 仍保留内存/教学用 embedded 模式。

## 1. 当前架构

```mermaid
flowchart LR
    Client[HTTP / SSE 客户端]
    API[FastAPI 路由与 Schema]
    TaskReads[公开任务 GET / list]
    SSE[公开 SSE]
    Live[公开 health/live]
    Ready[公开 health/ready]
    MetricsAPI[公开 metrics]
    ReadService[TaskService]
    EventService[TaskEventService]
    Probe[ReadinessProbe]
    MetricStore[MetricsRegistry]
    Writes[创建 / 取消 / 重试]
    Auth[Bearer 鉴权]
    Rate[创建 / 重试限流]
    Cancel[取消命令]
    Cost[创建 / 重试命令]
    Handler[TaskCommandHandler 端口]
    Runtime[embedded: TaskRuntime 实现端口]
    Persisted[api_only: PersistedTaskCommands 实现端口]
    Worker[独立 TaskWorker 进程]
    WorkerMonitor[Worker 私有健康与指标端点]
    WorkerMetrics[Worker MetricsRegistry]
    Claim[PostgresWorkerRepository]
    Lease[SKIP LOCKED / 租约恢复]
    Queue[asyncio.Queue]
    Gate[Semaphore]
    Executor[白名单确定性执行器]
    Repo[TaskRepository]
    EventRepo[TaskEventRepository]
    Memory[(内存仓储)]
    PG[(PostgreSQL)]
    Logs[安全 JSON Log]

    Client --> API
    API --> TaskReads --> ReadService --> Repo
    API --> SSE --> EventService --> EventRepo
    SSE --> Client
    API --> Live
    API --> Ready --> Probe
    Ready -. embedded accepting .-> Runtime
    API --> MetricsAPI --> MetricStore
    API --> Writes --> Auth
    Auth --> Cancel --> Handler
    Auth --> Rate --> Cost --> Handler
    Handler -. embedded 组合 .-> Runtime
    Handler -. api_only 组合 .-> Persisted
    Runtime --> Queue --> Gate --> Executor
    Runtime --> Repo
    Persisted --> Repo
    Worker --> Claim --> Lease --> PG
    Worker --> Executor
    Worker --> Logs
    Worker --> WorkerMetrics
    WorkerMonitor --> WorkerMetrics
    WorkerMonitor --> Probe
    WorkerMonitor -. 执行循环仍运行 .-> Worker
    Repo --> Memory
    Repo --> PG
    EventRepo --> Memory
    EventRepo --> PG
    Probe --> Memory
    Probe --> PG
    API --> Logs
    Runtime --> Logs
```

`main.py` 使用 `embedded` 模式，适合无数据库教学。Compose 的 API 使用 `api_only`，只持久化命令；`worker_main.py` 创建独立 Engine、`PostgresWorkerRepository` 和 `TaskWorker`，用 PostgreSQL 原子领取并推进任务。

## 2. 任务状态机

```mermaid
stateDiagram-v2
    [*] --> queued: 创建任务
    queued --> running: Worker 原子领取
    running --> succeeded: 提交成功
    running --> failed: 错误或超时
    failed --> queued: 自动或手动重试
    queued --> cancel_requested: 请求取消
    running --> cancel_requested: 请求取消
    cancel_requested --> cancelled: 执行清理完成
    succeeded --> [*]
    cancelled --> [*]
```

`succeeded` 与 `cancelled` 是终态。每次转换必须经过 `Task.transition_to()`；Repository 在同一临界区或事务中完成读取、校验、状态更新、attempt/event 写入。

## 3. PostgreSQL 表关系

```mermaid
erDiagram
    TASKS ||--o{ TASK_ATTEMPTS : "拥有执行尝试"
    TASKS ||--o{ TASK_EVENTS : "产生事件"

    TASKS {
        uuid id PK
        varchar task_type
        jsonb payload
        varchar status
        jsonb result
        text error
        varchar idempotency_key UK
        integer version
        timestamptz created_at
        timestamptz updated_at
        timestamptz available_at
        integer attempts_in_cycle
        varchar request_id
    }

    TASK_ATTEMPTS {
        uuid id PK
        uuid task_id FK
        integer attempt_number
        integer attempt_in_cycle
        varchar worker_id
        timestamptz started_at
        timestamptz lease_expires_at
        timestamptz finished_at
        varchar outcome
        text error
        timestamptz retry_at
        varchar request_id
    }

    TASK_EVENTS {
        bigint id PK
        uuid task_id FK
        varchar event_type
        varchar from_status
        varchar to_status
        jsonb payload
        timestamptz created_at
    }
```

关键约束：

- `tasks.idempotency_key` 的非空值唯一，解决并发重复创建；
- `task_attempts(task_id, attempt_number)` 唯一，保证同一任务的审计序号不重复；
- `task_attempts(task_id) WHERE finished_at IS NULL` 部分唯一索引保证每个任务最多一个活动 attempt；
- `task_attempts(outcome, lease_expires_at)` 支持过期租约扫描；
- 租约时间来自 PostgreSQL `clock_timestamp()`；完成或失败写入会在取得行锁后重新取数据库时间，防止锁等待跨过租约截止时间；Worker 传入时钟只用于计算重试延迟；
- `task_events.id` 全局递增，SSE 以 `Last-Event-ID` 作为已确认游标；
- `tasks.version` 记录状态版本，不替代数据库事务或行锁。

## 4. 请求到事件的关联链

```mermaid
sequenceDiagram
    participant C as Client
    participant A as FastAPI
    participant W as 独立 Worker
    participant P as Repository
    participant S as SSE

    C->>A: POST /v1/tasks + Bearer
    A->>A: 生成 request_id
    A->>P: 保存 queued + task_created
    A-->>C: 201 + task_id + request_id
    W->>P: FOR UPDATE SKIP LOCKED
    P-->>W: claim + attempt_id + worker_id + lease
    W->>P: running -> succeeded/failed/cancelled
    Note over W,P: 只接受仍有效且属于当前 Worker 的租约
    W->>P: 崩溃后不写完成
    W->>P: 替代实例扫描过期租约并重新领取
    C->>S: GET events + Last-Event-ID
    S->>P: 查询 id > cursor
    S-->>C: id/event/data
```

日志只输出白名单字段。`request_id`、`task_id`、`attempt_id`、`worker_id` 用于关联，不记录请求体、payload、API Key 或原始异常文本。

## 5. 已知边界

- PostgreSQL Worker 提供至少一次执行，不承诺对外部副作用“恰好一次”；
- 当前没有租约续期；配置要求租约时长大于单任务超时，长任务需先设计 heartbeat/续租；
- API 的 `/metrics` 只读取 API 进程；Worker 在私有 `127.0.0.1:9101/metrics` 提供其执行指标，两者均为单进程内存快照、不持久且不聚合；
- Worker 的 `/health/ready` 检查执行循环与数据库表；API 就绪不代表整个队列有 Worker 处理。执行循环意外结束时监控服务器也退出，供进程重启策略接管；
- `process_acceptance_demo` 提供本机真实强杀与真实租约等待路径；Docker 镜像、命名卷重启持久化和 Linux SIGTERM 已另行验收，见 `docker-acceptance.md`，不得混淆两种证据；
- 并发基线用于记录变化趋势，不是生产容量结论。
