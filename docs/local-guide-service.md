# 本机攻略服务与一次性迁移

## 服务和客户端

服务只从显式的 `GUIDE_DATABASE_URL` 读取新 PostgreSQL 库，启动不建表。请先备份旧库、建立独立空库，再运行 Alembic。维护者用已审核攻略快照导入：

```powershell
uv run --locked --env-file .guide-server.env python -m teyvatnotes.knowledge.cli import-snapshot "<快照.json>" --publish
```

先运行 `.\scripts\create_local_tls.ps1`（已有证书时复用，不覆盖），再运行 `.\scripts\start_local.ps1 -EnvFile .guide-server.env`。脚本固定只监听回环地址，不读取客户端转发头，不需要域名、反代或公网入口。若用 Compose，为容器显式配置 `GUIDE_COMPOSE_DATABASE_URL`、`GUIDE_POSTGRES_PASSWORD` 和服务端环境，运行 `docker compose --env-file .guide-server.env up --build`；它使用独立卷，PostgreSQL 不映射端口，HTTPS 仅映射宿主机回环。不要把已有 PostgreSQL 数据卷绑给新项目。

安装默认在 `%LOCALAPPDATA%\TeyvatNotes` 创建缺失的 `client.toml` 和可选 `service-ca.pem`。开发运行也使用同样位置；可用 `TEYVAT_NOTES_DATA_DIR` 指定隔离目录。服务只在有效请求需要答案时读取自己的 `LLM_*`，模型未配置不会伪装成资料不足。确认实际模型调用会计费。

### 攻略回答的输出上限

服务端启动时从 `.guide-server.env` 读取 `GUIDE_ANSWER_MAX_OUTPUT_TOKENS`，默认 8192，可设为 256–16384 的整数。修改后重启服务；Compose 部署需重新创建 API 容器。选择上限时还应考虑模型支持范围、耗时和费用。它不改变请求超时、字节大小限制或引用校验，也不会触发重试。

只有上游明确返回 `status=incomplete` 且 `incomplete_details.reason=max_output_tokens`，才会返回 `502 answer_output_limit_reached`。仅凭用量恰好等于上限不能判定截断；完整响应照常接受格式和引用校验。

## API

模型返回完整响应但正文不符合攻略格式时，客户端仍提示格式错误。用请求 ID 查服务端的 `guide_http` 日志：`validation_detail=json_invalid` 表示正文不是合法 JSON；`missing_field:claims[0].required_equipment` 表示第一条结论缺少必填字段；`status_conflict` 表示答案状态与内容矛盾。只记录首处分类和已知字段路径，不记录正文或自定义字段名。合法的 `insufficient_evidence` 是正常回答（HTTP 200），可给出缺口或资料冲突；它不同于输出超限或格式错误。

状态冲突还记录首条违反的规则、模型声明的状态和四组回答字段的条数。例如 `rule=answer_forbids_questions answer_status=answer claims_count=1 questions_count=1 gaps_count=0 conflicts_count=0` 表示最终答案同时带了追问。这些数值只统计回答字段，不统计玩家库存；普通访问日志不记录正文。旧日志若只有 `status_conflict`，无法追溯具体组合。补充条件是一次新提交，只发送当前问题和合并后的已确认条件：未确认持有名单为 `null`，明确没有为 `[]`，否则为实际名单。

**本机错误原文**：启动服务时终端会打印绝对路径。在 Windows 资源管理器地址栏输入 `%LOCALAPPDATA%\TeyvatNotes\server\logs`，打开 `guide-errors.jsonl` 并搜索请求 ID。每个问答 HTTP 错误一行 UTF-8 JSON，包括时间、与终端一致的错误摘要、`model_output`。若已取得送入校验器的回答，此字段逐字保留正文（换行按 JSON 转义）；否则标记“未收到模型输出”。成功回答不写此文件。文件逐条刷新、重启追加，约 5 MiB 轮转，最多保留 3 个旧文件（`.1` 最新）。Compose 将它持久化在独立的 `guide-server-state` 卷。该文件不提供客户端或 HTTP 下载入口。它不采集请求、请求头、提示词、推理过程或上游信封；模型回答本身仍可能复述玩家信息，请按私密资料保管，分享前先脱敏。

服务商支持 Responses 的 `json_schema` 时，维护者可在服务端显式开启 `LLM_STRUCTURED_OUTPUTS=true`，重启后生效。验收时应使用本项目 `JsonAnswerDecoder.schema()` 生成的完整 Schema 和虚构输入；一次成功仅证明该端点和模型接受当次请求，不替代本地状态、库存和引用校验。

- `POST /v1/guides/sessions`：空体或空 JSON 对象；成功 `201`，返回 `access_token`、带时区的 `expires_at`。令牌 7 天有效，服务端只保存 SHA-256 哈希。
- `POST /v1/guides/answer`：`Authorization: Bearer <令牌>`，JSON 请求体不超过 16 KiB。接收本轮 `question`、`guide_kind/entity/game_version/goal/keywords`、必要的条件、进度和持有清单，未知字段拒绝。不接受历史或连接信息。返回 `200` 的 `answer`、`needs_clarification` 或 `insufficient_evidence`；引用有版本、原文链接和证据文本。失败为 `code/message/request_id`，`X-Request-ID` 可用于定位，不回显正文。
- `/health/live` 检查进程，`/health/ready` 检查新库表。
- Windows 本机启动脚本和本机 Compose 选择 `GUIDE_DEPLOYMENT_MODE=local`：匿名令牌签发和模型调用不读取或修改每日计数，已有计数原样保留。令牌仍须有效，单实例最多同时调用 2 个模型请求，超时不自动重发。取消服务计数不代表取消模型服务商的限额或费用。不要将本机模式通过转发或反代暴露给其他用户。
- 其他部署默认 `GUIDE_DEPLOYMENT_MODE=public`：匿名令牌每 IP 每 UTC 日最多 3 个，全局最多 1000 个；模型每令牌 5 次、每 IP 20 次、全局 100 次。额度只在模型发送前扣减；配额耗尽返回 `429`，已开始的调用不因超时而退回。

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
