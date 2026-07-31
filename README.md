# Agent Runtime Foundation

这是一个用于学习可靠 Agent Runtime 基础能力的 Python 项目。项目会先使用确定性任务验证状态、失败、重试、取消和测试，再逐步接入 FastAPI、PostgreSQL、独立 Worker 与 SSE。

## 当前阶段

第 2 周：HTTP、REST 与 FastAPI。

当前实现包含：

- `TaskStatus`：任务状态枚举；
- `TaskResult`：任务执行结果；
- `Task`：任务实体及状态转换；
- `InvalidTaskTransition`：非法转换异常；
- 合法、非法和终态转换的参数化测试。
- `TaskRepository` 协议和线程安全的内存实现；
- 创建、查询、列表、取消和重试 API；
- 分页、状态过滤、Repository 依赖注入；
- 统一的 `code/message/details/request_id` 错误响应。

领域层仍不依赖 FastAPI、数据库或 LLM SDK。

## HTTP API

| 方法 | 路径 | 用途 |
| --- | --- | --- |
| `POST` | `/v1/tasks` | 创建任务 |
| `GET` | `/v1/tasks/{task_id}` | 查询任务 |
| `GET` | `/v1/tasks?status=queued&offset=0&limit=20` | 分页列表和状态过滤 |
| `POST` | `/v1/tasks/{task_id}/cancel` | 请求取消任务 |
| `POST` | `/v1/tasks/{task_id}/retry` | 重试失败任务 |

本地启动：

```powershell
uv run fastapi dev main.py
```

启动后可访问 `http://127.0.0.1:8000/docs` 查看 OpenAPI 交互文档。

当前允许的任务类型：

- `sum_numbers`：`payload.numbers` 包含 1～100 个整数，每个整数在 `-1000000～1000000` 之间；
- `word_count`：`payload.text` 是 1～10000 个字符的非空白文本。

### 调用示例

创建任务：

```powershell
curl.exe -i -X POST http://127.0.0.1:8000/v1/tasks `
  -H "Content-Type: application/json" `
  -d '{"task_type":"sum_numbers","payload":{"numbers":[1,2,3]}}'
```

成功时返回 `201 Created`，并在 `Location` 响应头中给出查询路径。

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
curl.exe -X POST http://127.0.0.1:8000/v1/tasks/<task_id>/cancel
```

重试失败任务：

```powershell
curl.exe -X POST http://127.0.0.1:8000/v1/tasks/<task_id>/retry
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
| `404` | `task_not_found` | 指定任务不存在 |
| `409` | `invalid_task_transition` | 当前状态不允许取消或重试 |
| `422` | `validation_error` | 请求体或查询参数不符合 Schema |
| `500` | `internal_error` | 未预期的服务端错误 |

## 分层与数据边界

```text
HTTP / FastAPI
  → Pydantic Schema
  → TaskService
  → Task 状态机
  → TaskRepository
```

路由只负责输入转换、调用用例和输出转换。取消与重试的合法性由第一周实现的 `Task.transition_to()` 保证，避免 API 层出现第二套状态规则。

当前使用内存 Repository：数据仅在单个应用进程内保留，服务重启后会丢失。数据库持久化留到后续阶段。

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
- uv。

## 安装与检查

```powershell
uv sync
uv run pytest -q
uv run ruff check .
uv run ruff format --check .
uv run mypy
```

## 项目结构

```text
src/agent_runtime_foundation/
├─ __init__.py
├─ application/
│  ├─ models.py
│  └─ task_service.py
├─ api/
│  ├─ app.py
│  ├─ error_handlers.py
│  ├─ schemas.py
│  └─ routes/tasks.py
├─ domain/
│  ├─ errors.py
│  └─ task.py
└─ repositories/
   ├─ base.py
   └─ memory.py

tests/
├─ test_api.py
├─ test_repository.py
└─ test_task.py
```
