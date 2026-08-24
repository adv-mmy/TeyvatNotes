# 阶段一演示与验收

## 1. 15 分钟启动路径

只验证 Python 能力时：

```powershell
uv sync --locked
uv run python -m scripts.stage_one_demo
uv run python -m scripts.concurrency_baseline --tasks 200 --concurrency 20
uv run pytest -q
```

验证 Compose 时，复制 `.env.example` 为 `.env`，填入随机 API Key 和 PostgreSQL 密码，然后运行：

```powershell
docker compose up --build -d
```

Compose 实际启动 API、内嵌 Worker、迁移服务与 PostgreSQL。没有完成独立 PostgreSQL Worker，不得把这一步描述为跨进程恢复验收。

## 2. 七个最终演示场景

`uv run python -m scripts.stage_one_demo` 会输出安全 JSON 日志和一份最终 JSON 报告：

| 要求 | 实际演示 | 报告证据 |
| --- | --- | --- |
| 正常创建并完成 | 通过受鉴权的 FastAPI `POST/GET` | `normal_status=succeeded`、`normal_total=6` |
| 重复幂等键 | 两次提交同一 `Idempotency-Key` | `idempotent_same_task=true` |
| 取消运行任务 | 可控执行器进入 running 后调用 cancel | `cancelled_status=cancelled` |
| 超时和自动重试 | `TaskWorker` 第一次超时、第二次成功 | `retry_scheduled, succeeded` |
| 强制结束 Worker | 启动阻塞执行器，确认进入执行后取消正在运行的 Worker Task | 恢复前任务/attempt 仍为 `running`、`metrics.completed=0`，日志为 `task_abandoned` 而非伪 `task_completed/cancelled` |
| 重启 Worker 恢复 | 新 Worker 在租约到期后运行 | `crash_recovery_worker_ids` 从 `worker-crashed` 变为 `worker-replacement`，第二条 attempt 为 `succeeded` |
| SSE 和日志解释 | 读取终态事件流并捕获、逐行解析安全 JSON 日志 | 递增 `sse_event_ids`、事件类型、`normal_request_id`、SSE/日志关联均为 true、密钥未出现 |

强制结束与恢复场景使用进程内 `WorkerRepository`：先真正启动一个阻塞 Worker，确认执行开始后取消该 asyncio Task，再以显式时钟推进租约并启动替代 Worker。它验证 Worker Python 策略、取消传播和 attempt 审计；真正跨进程 PostgreSQL 恢复仍等待第四周 SQL。

## 3. CI 验收

`.github/workflows/ci.yml` 有两个独立 Job：

- `quality`：锁文件安装、Ruff、格式、mypy、非 PostgreSQL 测试和并发基线；
- `postgres`：启动真实 PostgreSQL 17 服务、执行 Alembic 迁移，再运行 `postgres` 标记的 Repository 集成测试。

本地没有 PostgreSQL 时，8 项真实库测试会明确跳过；CI 中设置 `TEST_DATABASE_URL` 后必须实际运行，不能以 skip 代替通过。

## 4. 并发基线如何解读

并发脚本同时提交确定性任务，在真实 `await` 调度点记录 `peak_concurrency`，检查峰值没有突破 Semaphore 上限且所有任务最终为 `succeeded`，并输出总耗时与每秒吞吐。CI 把“并发峰值正确、任务全部收敛”作为门禁，不设置固定吞吐阈值，因为共享 Runner 的性能会抖动。

要比较版本变化，应在相同机器、相同 Python、相同参数下至少运行多轮，记录中位数和高分位，而不是只比较一次结果。当前基线是进程内内存实现，不代表 PostgreSQL 或生产网络容量。

## 5. 讲解任务生命周期

不看源码时应能说明：

1. API 校验 Bearer、任务类型、payload 和幂等键；
2. Repository 保存 `queued` 任务和创建事件；
3. Runtime/Worker 原子领取，写入 attempt 并转为 `running`；
4. Semaphore 控制真正执行并发，timeout 限制单次执行；
5. 成功、失败、取消都通过状态机和 Repository 收敛；
6. 可重试失败按最大次数与退避重新排队；
7. SSE 用递增事件 ID 续传，日志用四类关联 ID 串起请求和执行；
8. Worker 崩溃不能承诺恰好一次，只能依赖租约、至少一次执行和业务幂等。
