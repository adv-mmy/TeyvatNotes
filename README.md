# TeyvatNotes · 提瓦特手记

TeyvatNotes 是一个《原神》攻略问答项目，旨在帮助玩家根据游戏版本和自己的角色、装备情况查找合适的配队建议，并核对建议所依据的攻略原文。

## 快速启动

项目以 Docker Compose 在单台主机部署，用户从主机终端进入容器中的 `teyvatnotes` 交互式 CLI。

### 前提

- 已安装 Docker 与 `docker compose`（Compose V2）。
- 启动攻略库前，将 PostgreSQL 密码放在 `secrets/postgres_password`；问答还需在 `secrets/model_api_key` 中配置 DeepSeek API Key。两者都是本机文件，不提交到仓库。若放在其他位置，请在运行 Compose 的终端将 `TEYVAT_POSTGRES_PASSWORD_FILE`、`TEYVAT_MODEL_API_KEY_FILE` 分别设为对应的文件路径。
- 本地开发还需 Node.js >=24.15.0 <25。

首次使用或更新源码后，在项目目录构建 CLI 镜像：

```bash
docker compose build teyvatnotes
```

CLI 启动后可用命令：`/new`（新建会话）、`/open <会话 ID>`、`/history`、`/exit`。空白输入不会启动一轮；`Ctrl+C` 取消当前轮次。

### 离线查看历史

镜像已经构建时，可直接查看同一 Compose 项目里已保存的会话。不启动数据库，不挂载密钥，不连接网络：

```bash
docker compose run --rm teyvatnotes-offline
```

### V2：启动本地攻略问答

```bash
# 启动核心依赖（后台运行，等待就绪）
docker compose up -d --wait postgres chromadb ollama

# 初始化新版攻略库（仅首次或数据库结构升级时显式执行）
docker compose run --rm -T teyvatnotes db migrate

# 拉取配置中固定的 Embedding 模型
docker compose exec ollama ollama pull qwen3-embedding:0.6b

# 进入问答；启动后输入 /new 创建会话，再问带明确版本的配队问题
docker compose run --rm teyvatnotes-ask
```

问答入口读取本地攻略库，调用配置的回答模型；真实联网补查未启用。本地找不到适用攻略时，不会自动从网站抓取正文。

### 维护命令（在 CLI 容器内一次性运行）

维护入口 `teyvatnotes` 用于初始化数据库、导入和发布资料，不挂载回答模型密钥。

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
- 离线历史不依赖攻略库或模型；问答和导入需要对应服务就绪。
- 更多部署边界与阶段划分见 `docs/architecture.md#7-docker-compose-部署边界`、`docs/architecture.md#8-分阶段交付`。
