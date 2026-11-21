# TeyvatNotes · 提瓦特手记

共享原神攻略服务和 Windows 桌面客户端。桌面保存自己的会话，通过 HTTPS 向本机服务提问；PostgreSQL 攻略库和模型都由服务端管理。客户端无需数据库密码、模型密钥或会话 UUID。

## 本机运行

需要 Python 3.12+、uv 和 PostgreSQL。先创建一个**专用空数据库**，不要指向旧项目库或测试库。将 [.env.example](.env.example) 复制成未纳入 Git 的 `.guide-server.env`，填写 `GUIDE_DATABASE_URL`、至少 32 字节且固定不变的 `GUIDE_IP_HASH_KEY`，以及服务端的通用 `LLM_*` 模型配置。

首次在项目目录完成准备（PowerShell 7）：

```powershell
uv sync --locked
uv run --locked --env-file .guide-server.env alembic upgrade head
.\scripts\create_local_tls.ps1
```

### 启动与停止服务器

完成准备后，每次启动运行：

```powershell
.\scripts\start_local.ps1 -EnvFile .guide-server.env
```

**停止服务器：** 在运行上述命令的终端按 `Ctrl+C`，等待服务退出。再次执行启动命令即可重启；停止攻略服务不会关闭独立运行的 PostgreSQL。

服务只监听 `https://127.0.0.1:8443`。证书不写入系统信任库；只把 `build\local-tls\service-ca.pem` 提供给客户端，**不要分发服务端私钥**。

本机模式没有每日领取令牌或问答次数上限，但仍限制同时处理的模型请求；模型服务商的限额和计费不变。

若选择 Docker Compose，先按[本机操作说明](docs/local-guide-service.md)配置容器连接串和数据库密码，再使用以下命令；本机脚本与 Compose 启动方式二选一：

```powershell
# 后台启动服务与数据库
docker compose --env-file .guide-server.env up --build -d
# 停止本项目服务与数据库，保留容器和数据卷
docker compose --env-file .guide-server.env stop
```

## 准备攻略与使用桌面端

先按[资料入库说明](docs/guide-ingestion.md)导入并发布实际攻略。新库是空的，不随安装包提供攻略；匹配不到已发布证据时不调用模型。自动补查接口目前只用于隔离验收，真实米游社来源没有启用。

运行 `dist/desktop/TeyvatNotes-Setup.exe`。本机安装包预填服务地址并可携带公开证书；首次有效提问会自动领取匿名访问令牌。聊天历史保存在 `%LOCALAPPDATA%\TeyvatNotes\client\conversations.sqlite3`，断网仍可查看。新版不会读取旧桌面 `.env`，也不自动转换旧会话。

需要自行打包时，安装 Inno Setup 后执行：

```powershell
uv sync --locked --group desktop-build
.\scripts\build_desktop.ps1 -CaCertificate .\build\local-tls\service-ca.pem
```

已安装的 `client.toml` 和公开证书不会被覆盖。若复用不同证书，在确认新证书来源后由维护者显式更新。不要通过关闭 TLS 验证解决证书问题。

## 迁移与维护

独立迁移工具默认只预检。核对备份、源和目标后才加 `--apply`，不会覆盖冲突记录，也不会删除旧库。命令、接口、限额和故障处理见[本机操作与迁移](docs/local-guide-service.md)。
