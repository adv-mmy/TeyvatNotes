# 本机攻略服务与一次性迁移

## 服务和客户端

服务只从显式的 `GUIDE_DATABASE_URL` 读取新 PostgreSQL 库，启动不建表。请先备份旧库、建立独立空库，再运行 Alembic。维护者用已审核攻略快照导入：

```powershell
uv run --locked --env-file .guide-server.env python -m teyvatnotes.knowledge.cli import-snapshot "<快照.json>" --publish
```

先运行 `.\scripts\create_local_tls.ps1`（已有证书时复用，不覆盖），再运行 `.\scripts\start_local.ps1 -EnvFile .guide-server.env`。脚本固定只监听回环地址，不读取客户端转发头，不需要域名、反代或公网入口。若用 Compose，为容器显式配置 `GUIDE_COMPOSE_DATABASE_URL`、`GUIDE_POSTGRES_PASSWORD` 和服务端环境，运行 `docker compose --env-file .guide-server.env up --build`；它使用独立卷，PostgreSQL 不映射端口，HTTPS 仅映射宿主机回环。不要把已有 PostgreSQL 数据卷绑给新项目。

安装默认在 `%LOCALAPPDATA%\TeyvatNotes` 创建缺失的 `client.toml` 和可选 `service-ca.pem`。开发运行也使用同样位置；可用 `TEYVAT_NOTES_DATA_DIR` 指定隔离目录。服务只在有效请求需要答案时读取自己的 `LLM_*`，模型未配置不会伪装成资料不足。确认实际模型调用会计费。

## API

- `POST /v1/guides/sessions`：空体或空 JSON 对象；成功 `201`，返回 `access_token`、带时区的 `expires_at`。令牌 7 天有效，服务端只保存 SHA-256 哈希。
- `POST /v1/guides/answer`：`Authorization: Bearer <令牌>`，JSON 请求体不超过 16 KiB。接收本轮 `question`、`guide_kind/entity/game_version/goal/keywords`、必要的条件、进度和持有清单，未知字段拒绝。不接受历史或连接信息。返回 `200` 的 `answer`、`needs_clarification` 或 `insufficient_evidence`；引用有版本、原文链接和证据文本。失败为 `code/message/request_id`，`X-Request-ID` 可用于定位，不回显正文。
- `/health/live` 检查进程，`/health/ready` 检查新库表。匿名领令牌每 IP 每 UTC 日 3 次，另有全局签发保护；模型每令牌 5 次、每 IP 20 次、全局 100 次，单实例同时最多 2 个。额度只在模型发送前扣减；配额耗尽返回 `429`，已开始的调用不因超时而退回，也不自动重发。

## 迁移

先关闭旧客户端或使用一致性备份。源库使用只读权限，`GUIDE_LEGACY_DATABASE_URL` 和目标 `GUIDE_DATABASE_URL` 必须指向不同名称的数据库；目标须已执行新基线。每条命令省略 `--apply` 时只预检。

```powershell
# 复制攻略来源、版本、证据块及检查点。
uv run --locked --env-file .guide-server.env python -m scripts.migrate_guide_data guides
# 导入旧 SQLite 会话。
uv run --locked python -m scripts.migrate_guide_data conversations --sqlite "$env:LOCALAPPDATA\TeyvatNotes\conversations.sqlite3"
# 或从旧 PostgreSQL 读取明确指定的所有者分区。
uv run --locked --env-file .guide-server.env python -m scripts.migrate_guide_data conversations --postgres --owner "<旧所有者UUID>"
```

报告只含数量和稳定错误，不输出源内容。确认预检后在相同命令加 `--apply`，再对照数量并抽查引用。会话原 ID、回答和引用保留，迁入目标本地用户，未完成轮次标记为中断；不会重放。冲突时回滚，不覆盖目标。若需退回旧部署，用旧程序和原数据，不尝试降级新库，也不删除任何文件或数据卷。
