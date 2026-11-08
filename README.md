# TeyvatNotes

一个 Python 项目，提供异步任务和攻略问答：

- **异步任务 API**：提交、查询、取消和重试任务，支持求和与词数统计。
- **知识问答 CLI**：从资料中检索证据并生成带引用的攻略回答，提供无需模型服务的离线演示。
- **提瓦特手记桌面版**：在 Windows 窗口中提问、补充玩家条件、核对引用和保存模型配置。

## 快速开始

安装 Python 3.12+ 和 uv，在仓库根目录运行：

```powershell
uv sync --locked
```

然后任选一个入口试用。以下命令以 PowerShell 为例。

### 试用任务 API

生成一个本地测试密钥：

```powershell
uv run python -c "import secrets; print(secrets.token_urlsafe(32))"
```

将生成的值填入下方命令，启动内存版服务：

```powershell
$env:AGENT_RUNTIME_API_KEY = "<生成的密钥>"
$env:AGENT_RUNTIME_EXECUTION_MODE = "embedded"
$env:DATABASE_URL = ""
uv run fastapi dev main.py
```

1. 打开 [接口文档](http://127.0.0.1:8000/docs)，点击 **Authorize**，填入同一个密钥。
2. 展开 `POST /v1/tasks`，点击 **Try it out**，提交以下 JSON：

   ```json
   {"task_type":"sum_numbers","payload":{"numbers":[1,2,3]}}
   ```

3. 从响应复制 `id`，用 `GET /v1/tasks/{task_id}` 查询。当状态为 `succeeded` 时，`result.output.total` 应为 `6`。

此方式无需数据库，重启后任务不会保留。查询和事件订阅接口目前没有用户级访问隔离，请仅在可信环境中试用。

### 试用知识问答 CLI

无需启动任务服务，也无需数据库、模型密钥或付费调用：

```powershell
uv run python -m teyvatnotes.knowledge.answer_cli samples/questions/character.json --demo
```

命令会输出一份含回答和引用的 JSON，并标记 `demo_only: true`。示例使用虚构资料，只用于体验流程。

### 使用 Windows 桌面版

运行 `dist/desktop/TeyvatNotes-Setup.exe`，安装后从开始菜单打开「提瓦特手记」。首次启动进入虚构演示，可以尝试补条件、查看引用和换队友。安装包包含 Python 和 Qt 运行时。

在「设置」里可以编辑 HTTPS Base URL、模型型号和 API Key，保存后下一轮真实问答使用新值。默认配置位于 `%LOCALAPPDATA%\TeyvatNotes\.env`，也可选用项目已有的 `.env`。要使用真实资料，配置文件需包含业务 `DATABASE_URL` 和 `GUIDE_SESSION_OWNER_ID`，并使用已迁移的 PostgreSQL 及已审核发布的资料。完成后点击右上角的「切换真实资料」。升级或卸载时，用户配置保存在独立的数据目录。

开发时在仓库根目录运行：

```powershell
uv sync --locked --group desktop
uv run --no-sync python -m teyvatnotes.desktop
```

在 Windows x64 上构建安装包时，安装 [Inno Setup](https://jrsoftware.org/isdl.php)，然后运行：

```powershell
uv sync --locked --group desktop-build
.\scripts\build_desktop.ps1 -InnoCompiler "<ISCC.exe 的绝对路径>"
```

## 下一步

- **部署持久化任务服务**：[Docker Compose 启动说明](docs/stage-one-acceptance.md) · [环境变量示例](.env.example) · [故障恢复](docs/recovery-runbook.md)
- **使用自己的资料和模型**：[资料入库](docs/guide-ingestion.md) · [模型配置与单轮问答](docs/model-answering.md) · [多轮会话](docs/conversations.md)
- **了解实现与验证**：[任务架构](docs/architecture.md) · [检索方式](docs/rag-retrieval.md) · [评测说明](docs/evaluation.md)
