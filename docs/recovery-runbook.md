# 故障恢复手册

适用版本：`0.8.0`。本手册优先保护数据和可解释性；任何删除卷、删表或覆盖数据的操作都不属于常规恢复步骤。

## 1. 先收集证据

```powershell
docker compose ps
docker compose logs --since 15m postgres migrate api
curl.exe -i http://127.0.0.1:8000/health/live
curl.exe -i http://127.0.0.1:8000/health/ready
```

记录发生时间、HTTP 状态、`request_id/task_id/attempt_id/worker_id` 和容器退出码。不要复制 Authorization 头、`.env`、payload 或数据库密码到工单和聊天记录。

## 2. 快速判断

| 现象 | 首要检查 | 安全动作 |
| --- | --- | --- |
| `/health/live` 失败 | `api` 是否运行、端口是否占用、是否有非终态任务 | 停止写流量并盘点影响，确认接受当前恢复边界后再重启 |
| live 成功、ready 503 | PostgreSQL 健康、迁移、三张表、非终态任务 | 先恢复数据库和迁移；除非确有必要，不重启 API |
| `migrate` 退出非 0 | `DATABASE_URL`、迁移日志、版本表 | 修正配置后单独重跑迁移 |
| 创建返回 401 | Bearer 头与部署密钥是否一致 | 轮换/修正客户端配置，不打印密钥 |
| 创建返回 429 | `Retry-After` | 等待窗口恢复；不要用多容器绕过限流 |
| 任务长期 queued/running | 当前是否为内嵌 Runtime、API 是否重启 | 保留记录；按“任务未收敛”处理 |
| PostgreSQL 卷异常 | 磁盘、权限、备份 | 停止写入并从已验证备份恢复 |

## 3. PostgreSQL 不可用

1. 暂停新的写请求，记录故障窗口内已经返回成功的任务 ID；
2. `docker compose ps postgres` 检查健康状态；
3. `docker compose logs postgres` 检查认证、磁盘和启动错误；
4. 确认 `.env` 中用户名、数据库名和密码与已初始化卷一致；
5. 数据库恢复健康后执行 `docker compose run --rm migrate`；
6. 在 API 仍存活时先观察 readiness，并盘点 `queued/running/cancel_requested` 任务；
7. readiness 恢复后直接恢复流量，不做无意义重启；只有 API 已失效且已经记录非终态任务、明确接受当前无法自动扫描恢复的边界后，才执行 `docker compose restart api`；
8. readiness 返回 200 后逐项核对故障窗口内任务，再恢复写流量。

当前内嵌 Runtime 的队列在进程内。重启 API 会丢失队列引用，PostgreSQL 中已有的 `queued/running` 任务不会被自动扫描；因此“重启试试”不是无条件安全的数据库恢复动作。

不要用 `docker compose down -v` 处理认证或迁移问题；它会删除命名卷，且不属于可恢复的常规操作。

## 4. 迁移失败

1. 保存 `migrate` 完整日志和当前 Git 提交号；
2. 确认失败发生在连接、DDL 还是数据约束阶段；
3. 不要在不理解影响时手工修改 `alembic_version`；
4. 修复配置或代码后重新运行 `docker compose run --rm migrate`；
5. 使用 `/health/ready` 验证 `tasks`、`task_attempts`、`task_events` 均可访问。

当前只有初始迁移。以后出现破坏性迁移时，应先用临时数据库恢复备份并演练升级/回退，再操作真实数据。

## 5. 任务未收敛

先通过 `GET /v1/tasks/{task_id}` 与 SSE 事件确认最后状态和关联 ID。

- `queued`：确认内嵌 Runtime 正在接受任务；进程重启会丢失内存队列，当前不会自动扫描 PostgreSQL queued 任务；
- `running`：确认 API 是否在执行期间被强制结束。当前 Compose 还没有 PostgreSQL 租约恢复实现，不要直接把状态改成 succeeded；
- `cancel_requested`：等待执行清理；若进程已消失，保留证据并等待 PostgreSQL Worker SQL 补齐后的恢复工具；
- `failed`：读取公开错误与 attempt/event；只有业务上允许时才调用手动重试。

内存 Worker 的租约恢复可通过以下命令演练：

```powershell
uv run python -m scripts.stage_one_demo --no-logs
```

报告中的 `lease_expired -> succeeded` 表示旧 Worker 领取后未提交，新 Worker 在租约到期后恢复并成功执行。它证明 Python 策略，不证明 PostgreSQL 跨进程 SQL 已完成。

## 6. 安全轮换 API Key

1. 生成新的 URL-safe 随机值；
2. 在私有部署配置中替换 `AGENT_RUNTIME_API_KEY`；
3. 重建或重启 API 容器；
4. 让客户端切换新凭据并验证写接口；
5. 检查日志中不存在旧值或新值。

当前实现只有单密钥，不支持无中断双密钥轮换。需要零停机时，应先扩展配置模型和测试，不能临时把密钥写进代码。

## 7. 数据备份与恢复原则

- 命名卷不是备份；定期使用 `pg_dump` 生成加密、受访问控制的备份；
- 每份备份必须在临时数据库实际恢复并执行集成测试；
- 恢复前记录当前卷、镜像版本、迁移版本和 Git 提交；
- 恢复完成后运行迁移、readiness、幂等和分页检查；
- 删除旧卷或旧备份前必须得到明确批准。

## 8. 故障演练完成标准

- 能在 5 分钟内判断是 API、配置、迁移还是 PostgreSQL 故障；
- 能用关联 ID 还原任务最后一次已确认事件；
- 恢复过程中没有泄漏密钥、伪造成功状态或删除卷；
- 记录本次故障的触发方式、证据、恢复步骤、耗时和后续修复。
