# Agent Runtime Foundation

这是一个用于学习可靠 Agent Runtime 基础能力的 Python 项目。项目会先使用确定性任务验证状态、失败、重试、取消和测试，再逐步接入 FastAPI、PostgreSQL、独立 Worker 与 SSE。

## 当前阶段

第 1 周：Python 工程结构与任务状态机。

当前实现包含：

- `TaskStatus`：任务状态枚举；
- `TaskResult`：任务执行结果；
- `Task`：任务实体及状态转换；
- `InvalidTaskTransition`：非法转换异常；
- 合法、非法和终态转换的参数化测试。

本阶段的领域层不依赖 FastAPI、数据库或 LLM SDK。

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
uv run ruff format --check src tests
uv run mypy src
```

## 项目结构

```text
src/agent_runtime_foundation/
├─ __init__.py
└─ domain/
   ├─ __init__.py
   ├─ errors.py
   └─ task.py

tests/
└─ test_task.py
```
