# Docker 实际验收记录

日期：2026-09-30。源码基线：第八周与第四周 PostgreSQL 补全，版本 `0.8.1`。

## 1. 环境和隔离方式

- Docker Desktop 的 Linux/amd64 引擎，Docker Engine 29.8.1、Compose 5.5.1。
- 按项目 Dockerfile 构建，Python 3.13.5、uv 0.11.28；安装使用锁文件和 `--no-dev`。
- 原始验收项目：`agent-runtime-acceptance-20260930`。
- 修复后验收项目：`agent-runtime-acceptance-fixed-20260930`。
- 跨容器挂载验证：`agent-runtime-acceptance-volume-check-20260930`，使用 `--network none`，只在原数据库停止后挂载同一测试卷。
- 凭据仅在验收进程中随机生成并传入容器；未修改私有 `.env`，未连接本机 PostgreSQL。
- 两个项目使用各自命名卷；未执行 `down`、`rm`、`prune` 或删卷操作。

## 2. 实际结果

| 检查 | 结果 |
| --- | --- |
| 镜像构建、锁文件安装、Compose 启动 | 通过 |
| PostgreSQL → Alembic → API/Worker 启动依赖 | 通过，迁移服务退出码 0，版本 `20260825_0002` |
| PostgreSQL、API、Worker 健康检查 | 三者均 healthy |
| 正常任务 | 求和 60、词数 3 |
| 幂等与鉴权 | 相同幂等键返回同一任务；无凭据写请求返回 401 |
| SSE 精确续传 | 正常任务事件 1～5；重启场景旧游标 11 后只返回 12～15 |
| 非 root、只读 | 实际 UID/GID 10001；尝试写 `/app` 得到 `EROFS` |
| 私有配置未打入镜像 | `/app/.env` 不存在；随机密钥/连接串/密码未出现于应用日志和镜像历史 |
| Worker 私有监控 | `/health/ready`、真实成功指标可读取；重启后指标重新从 0 计数 |
| 跨进程日志 | API 创建请求与重启后 Worker 完成日志使用同一 request_id |
| 命名卷重启保留 | PostgreSQL stop/start 前后，两个 succeeded 和一个 queued 的 ID、状态及迁移版本不变 |
| 命名卷跨容器保留 | 原数据库停止后，新建无网络容器挂载原卷，三个任务 ID/成功状态与迁移版本均保留 |
| 排队任务恢复 | API 先重启仍显示 queued；Worker 重启后成功，结果 15 |
| API 非终态 SSE + SIGTERM | 修复后约 5.634 秒退出，未被 SIGKILL |
| 空闲 Worker + SIGTERM | 初次约 0.451 秒退出，未被 SIGKILL |
| 全量自动化质量门 | 219 项通过（含真实 PostgreSQL）；Ruff、70 文件格式、54 源文件 mypy 均通过 |

## 3. 发现并修复的问题

原 Dockerfile 使用 `fastapi run main.py`，没有 HTTP graceful shutdown 上限。当 Worker 停止且 SSE 仍订阅 queued 任务时，API 等待约 15.531 秒后被 Docker SIGKILL，退出码为 137。

当前默认启动命令为 exec-form：

```dockerfile
CMD ["uvicorn", "api_main:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers", "--timeout-graceful-shutdown", "5"]
```

- `api_main:app` 强制使用 API-only；教学用 `main.py` 仍保留 embedded 模式。
- 当前 FastAPI CLI 没有该停机参数，实际安装的 Uvicorn CLI 支持它，因此直接使用 Uvicorn。
- 5 秒限制 HTTP/SSE 连接等待，超时后取消连接、继续 lifespan 清理；Compose 仍留出 15 秒总宽限期。
- 新增 `test_docker_api_shutdown_is_bounded_before_container_sigkill`，解析 CMD 并核对两个超时；修复前测试失败，修复后通过。

修复后的 API、Worker 退出码为 143。当前 Uvicorn 在完成信号处理流程后重新触发 SIGTERM，因此不能只凭非零退出码判定异常；需要结合退出耗时、信号、服务器实现和生命周期测试。它与 Docker 超时强杀的 137 不同。安全日志把 Uvicorn 的原始消息统一为“ASGI 服务器事件”，本次没有把英文 shutdown 日志原文作为验收依据。

## 4. 保留资源和复核

验收结束时两个项目的应用与数据库容器，以及额外的跨容器卷验证容器均已停止，镜像、容器和命名卷保留，不占用 HTTP 端口。需要删除时先获得用户批准。

只读检查命令：

```powershell
docker ps -a --filter label=com.docker.compose.project=agent-runtime-acceptance-fixed-20260930
docker volume ls --filter label=com.docker.compose.project=agent-runtime-acceptance-fixed-20260930
docker inspect --format '{{.State.Status}} exit={{.State.ExitCode}}' agent-runtime-acceptance-fixed-20260930-api-1
docker inspect --format '{{.State.Status}} exit={{.State.ExitCode}}' agent-runtime-acceptance-volume-check-20260930
```

不要输出完整 `docker inspect` 或 `docker compose config` 到聊天记录，它们可能展开容器凭据。只验证配置可解析时使用 `docker compose config --quiet`。正常部署仍使用自己的 `.env`，不要覆盖已经配置的文件，也不要把验收凭据写入源码。

## 5. 证据边界

- 本次验证了 stop/start 与新容器挂载同一命名卷，但保留原数据库容器，未模拟删除数据卷。任何时候都没有两个运行的数据库同时挂载该卷。
- Worker 容器的 SIGTERM 实测针对空闲执行循环；执行中合作式停止由现有 Python 生命周期测试覆盖，未宣称容器内任意阻塞执行器都能有界退出。
- 容器内真实强杀 running 任务的租约恢复没有新增实测；对应逻辑已有真实 PostgreSQL、独立 OS 进程强杀和跨 Engine 验收。
- 数据库不可用、断电、磁盘耗尽和备份恢复不是本次验证范围。
- 本地 Docker 与质量门通过不代表远端 GitHub Actions 已执行；本次未提交或推送 Git。
