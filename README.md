# TeyvatNotes · 提瓦特手记

TeyvatNotes 是一个《原神》攻略问答项目，旨在帮助玩家根据游戏版本和自己的角色、装备情况查找合适的配队建议，并核对建议所依据的攻略原文。

## 快速启动

项目以 Docker Compose 在单台主机部署，用户从主机终端进入容器中的 `teyvatnotes` 交互式 CLI。

### 前提

- 已安装 Docker 与 `docker compose`（Compose V2）。
- 凭据文件放在 `secrets/` 目录（已入 `.gitignore`，不进仓库）：
  - `secrets/postgres_password`：PostgreSQL 密码（V2+）。
  - `secrets/model_api_key`：DeepSeek 回答模型 API Key（问答需要）。
  - `secrets/search_api_key`：DeepSeek Search/Fetch API Key（联网补查需要）。
  - `secrets/source_policy.json`：联网来源许可配置（联网补查需要）。
- 本地开发还需 Node.js >=24.15.0 <25。

### V1：只运行 CLI（无需数据库等依赖）

构建并进入交互式 CLI：

```bash
docker compose run --rm teyvatnotes
```

CLI 启动后可用命令：`/new`（新建会话）、`/open <会话 ID>`、`/history`、`/exit`。空白输入不会启动一轮；`Ctrl+C` 取消当前轮次。

仅查看已保存的本地历史（不启动依赖容器，也不要求模型联通）：

```bash
docker compose run --rm --no-deps teyvatnotes
```

### V2：启动攻略 RAG 依赖后使用 CLI

```bash
# 启动核心依赖（后台运行，等待就绪）
docker compose up -d --wait postgres chromadb ollama

# 初始化新版攻略库（仅首次或数据库结构升级时显式执行）
docker compose run --rm -T teyvatnotes db migrate

# 拉取配置中固定的 Embedding 模型
docker compose exec ollama ollama pull qwen3-embedding:0.6b

# 进入交互式 CLI
docker compose run --rm teyvatnotes
```

### 维护命令（在 CLI 容器内一次性运行）

```bash
docker compose run --rm -T teyvatnotes guides import /imports/guide.md        # 导入 Markdown/JSON 攻略，默认待发布
docker compose run --rm -T teyvatnotes guides import /imports/guide.md --publish  # 导入并直接发布
docker compose run --rm -T teyvatnotes guides publish <文档版本 ID>          # 发布已导入的文档版本
```

导入文件需挂载到容器内，或直接在容器内可读取的路径下提供；详见 `compose.yaml` 的 `teyvatnotes` 服务卷与 `main.ts` 的导入逻辑。

### 停止与重建

```bash
docker compose down
```

命名卷（`session_data`、`pg_data`、`chroma_data`、`ollama_models`）会保留，会话、攻略原文、向量索引和已下载的 Ollama 模型在容器重建后仍可用。升级前请备份会话和 PostgreSQL 数据；不将删除卷作为升级步骤。

### 本地开发

```bash
npm install
npm run build        # TypeScript 编译，输出 dist/
npm run typecheck    # 类型检查
npm run check        # Biome 格式与静态检查
npm run test         # 编译并运行测试
```

编译后的 CLI 入口为 `dist/src/bootstrap/main.js`（`package.json` `bin.teyvatnotes`）。

### 限制

- 默认不将 PostgreSQL、ChromaDB、Ollama、Redis 等内部服务端口映射到主机，不提供公网入口；容器间通过 Compose 服务名寻址。
- 凭据在运行时通过 Compose `secrets` 注入，不写入镜像或仓库。
- V1 可在没有 V2 依赖服务的情况下运行 CLI 和查看历史；问答、导入、检索等需要对应阶段服务就绪后方可用。
- 更多部署边界与阶段划分见 `docs/architecture.md#7-docker-compose-部署边界`、`docs/architecture.md#8-分阶段交付`。
