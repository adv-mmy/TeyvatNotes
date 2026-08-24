# 阶段一架构、状态机与表关系

本页描述 `0.8.0` 的实际实现，不把尚未完成的 PostgreSQL 独立 Worker 能力画成现状。

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
    Repo --> Memory
    Repo --> PG
    EventRepo --> Memory
    EventRepo --> PG
    Probe --> Memory
    Probe --> PG
    API --> Logs
    Runtime --> Logs
```

`main.py` 使用 `embedded` 模式，API 与进程内 Runtime 同进程运行。`api_main.py` 是只持久化命令的 `api_only` 入口；在 PostgreSQL Worker 原子领取 SQL 完成前，它不能单独推进任务。

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
    }

    TASK_ATTEMPTS {
        uuid id PK
        uuid task_id FK
        integer attempt_number
        timestamptz started_at
        timestamptz finished_at
        text error
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
- `task_events.id` 全局递增，SSE 以 `Last-Event-ID` 作为已确认游标；
- `tasks.version` 记录状态版本，不替代数据库事务或行锁。

## 4. 请求到事件的关联链

```mermaid
sequenceDiagram
    participant C as Client
    participant A as FastAPI
    participant R as Runtime/Worker
    participant P as Repository
    participant S as SSE

    C->>A: POST /v1/tasks + Bearer
    A->>A: 生成 request_id
    A->>P: 保存 queued + task_created
    A-->>C: 201 + task_id + request_id
    R->>P: queued -> running + attempt_id
    Note over R,P: 内存 WorkerRepository 的 claim 另记录 worker_id；PostgreSQL 表暂未持久化该字段
    R->>P: progress 0%
    R->>P: running -> succeeded/failed/cancelled
    R->>P: progress 100%（成功）
    C->>S: GET events + Last-Event-ID
    S->>P: 查询 id > cursor
    S-->>C: id/event/data
```

日志只输出白名单字段。`request_id`、`task_id`、`attempt_id`、`worker_id` 用于关联，不记录请求体、payload、API Key 或原始异常文本。

## 5. 已知边界

- Compose 当前运行 `API + 内嵌 Worker + PostgreSQL`，并非独立 PostgreSQL Worker；
- 强制中断与租约恢复由内存 `WorkerRepository` 演示和测试，跨进程恢复等待第四周 SQL；
- `/metrics` 是单进程内存快照，不持久、也不跨容器聚合；
- 并发基线用于记录变化趋势，不是生产容量结论。

内存 `AttemptRecord` 还包含 `worker_id/outcome/lease_expires_at/retry_at` 等租约审计信息；这些字段尚未进入 PostgreSQL ORM 和迁移，因此没有出现在上面的数据库 ER 图中。
