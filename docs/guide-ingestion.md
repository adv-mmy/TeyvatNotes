# 攻略资料入库规范：第二阶段第 1 周

当前入口读取本地 JSON 快照。真实 B 站和米游社页面、字幕访问、分 P 对齐与图文完整性需要用实际样本验证。

## PostgreSQL 配置

使用现有 PostgreSQL 时，在本地 `.env` 配置 `DATABASE_URL`，核对目标库并备份后运行 `uv run --env-file .env alembic upgrade head`。已有攻略库沿原修订链升级。历史任务表由前两条迁移保留，攻略 ORM 与 Alembic 自动生成迁移仅管理攻略相关表。

也可以用 Compose 启动本机数据库。将 `.env.example` 中的 `POSTGRES_*` 设置填入 `.env`，设置自己的 `POSTGRES_PASSWORD`，然后运行以下命令。

```powershell
docker compose up -d postgres
docker compose run --rm --build migrate
```

本机 CLI 的 `DATABASE_URL` 使用 `postgresql+asyncpg://<用户名>:<URL编码的密码>@127.0.0.1:55432/<数据库名>`。如果设置了 `POSTGRES_PORT`，使用对应端口。Compose 继续使用原项目名与 `postgres-data` 命名卷，已有数据可沿用。旧版部署如仍有任务容器运行，可用旧版 Compose 配置正常停止对应容器。

## 1. 输入与可信边界

- 文件必须为 UTF-8（允许 BOM），单文件最多 1 MiB；未知字段拒绝。
- 仅支持 `bilibili`、`miyoushe`，来源是规范 HTTPS 链接，不接受 URL 凭据。
- `native_id` 与链接路径一致；B 站显式 `part_id` 与链接的 `p` 参数一致。
- 链接跟踪参数不进入已保存快照；图片仅保存 HTTPS 引用，不下载、不进行 OCR/视觉解析。
- `complete` 默认 false，必须由整理者显式确认；字幕/正文来自人工准备，不声称程序已验证网站完整性。
- 标题、作者、适用游戏版本、实体、条件、正式/前瞻性质及证据类别由整理者提供，不由模型猜测。
- `published_at` 若提供必须含时区；发布时间与游戏版本是两个独立字段。

长期样本：`samples/guides/bilibili_character.json`、`samples/guides/miyoushe_quest.json`。它们的身份、内容均用于教学，`is_fixture=true`，不能发布。真实游戏版本、角色、任务与精选来源尚未确定。

## 2. 两种快照

B 站输入包含 `segments`，每项有 `start/end/text/section`。时间必须有限、非负、结束晚于开始、按顺序排列且不重叠。解析后保留分 P、字幕段索引和起止时间。这里不读取网站、字幕文件、音轨或关键帧。

米游社输入包含 `article_blocks`，每块为 `heading/paragraph/image`。必须保留原文顺序；解析后记录章节、块索引、段落索引和图片索引。图像块可带人工说明，但它不是程序识别出的画面事实。`requires_visual=true` 时视为缺少关键视觉证据。

## 3. 来源与不可变版本

来源身份是平台、原生 ID、分 P 的组合；来源 UUID 确定性生成。

文档版本由已规范化快照内容哈希及处理配置决定。标题、元数据或内容变更会创建新版本；解析器版本或字符上限变更也创建新版本。自定义解析器必须显式指定 `parser_version`，修改算法时由开发者升级版本。

`created_at` 是首次注册时的导入时间。文章发布时间保存在快照中；当前没有自动监测文章更新或版本兼容性推断。

四张新表：

| 表 | 职责 |
| --- | --- |
| `guide_sources` | 来源稳定身份及规范 URL |
| `guide_documents` | 不可变快照、元数据、处理版本及草稿/发布状态 |
| `guide_chunks` | 有序文本、章节、原文位置及字符偏移 |
| `guide_ingestion_steps` | 每阶段输入哈希、结果、完成/失败状态及稳定错误代码 |

第三个 Alembic 迁移负责创建这四张表。运行正式 CLI 前需要显式执行迁移。

## 4. 切块与检查点

流程：注册来源/版本 → parse → chunk → validate → 可选显式 publish。

- 默认每块最多 600 字符，可配置 100～2000；这是字符预算，不是 Token 预算。
- 每个正文块独立切分，不混合不同章节；保留原始字符区间和位置。
- 字幕章节来自整理者的 `section`，文章章节来自 heading。当前尚未自动识别技能/装备类别，未实现任务父步骤召回、关键词索引或向量检索。
- 已完成检查点只有输入哈希一致才可复用；完成结果不可被失败记录覆盖。
- 解析异常记录稳定失败代码；存储失败不伪造完成阶段。重新导入可从最后一个已完成检查点继续。
- PostgreSQL 的切块及 CHUNK 完成记录在同一事务提交；文档行锁串行化同一版本写入。
- 内存仓储使用 RLock 和 deepcopy。frozen dataclass 不冻结内部字典，不能代替复制隔离。
- 取消继续传播 CancelledError，不错误地写成普通失败。

导入中断后，再次通过 CLI 导入同一份快照即可从检查点恢复。

## 5. 发布规则

导入默认 draft；`--publish` 是人工发起的显式动作，存储层原子检查：三个阶段完成、有文本块、无阻断项、游戏版本已知、非虚构样本、正文已声明完整。

常见阻断代码：`version_unknown/fixture_source/source_incomplete/no_text_evidence/visual_evidence_missing`。

2026-10-04 按用户要求，体验服和正式服不作准入区分。`content_nature=released/preview` 仍记录来源声明，不作为阻断项；版本、非虚构身份、完整性、视觉缺口和显式审核发布规则不变。处理策略升级为 `offline-v2`，旧检查点不可覆盖，重新导入生成新文档版本。数据库 `0006` 仅替换发布元数据 CHECK；需使用者显式升级，不自动迁移业务库。回滚旧 CHECK 时，已有已发布 preview 会使回滚失败，不能为回滚改标签、删资料或降为草稿。此更新当轮暂缓真实数据库验证；2026-10-05在新建隔离验证库补验升级及发布/检索契约通过，没有回滚或升级业务库，见 [补验记录](postgres-wiki-acceptance.md)。

未知发布时间、只保存图片引用属于警告；图片含关键步骤时额外产生视觉缺口阻断。空字幕不触发自动 ASR，保留缺口等待后续实现。

后续专名校对更新采用 `offline-v3`：开发者词表支持角色、武器、圣遗物标准名和已确认误识别写法，解析前规范化实体、分块前校对正文，保存原文与完整词表哈希。可选 `recognized_terms` 显式列出识别到的分类名称；未确认项或正文歧义产生 `terminology_review_required`，不能发布。词表变化后重新导入生成新身份，不覆盖旧证据。词表未登记的正文名称并未自动全部识别；维护流程及保守中文边界见 [专名校对](terminology.md)。

这些规则校验的是结构和整理者声明，并不能证明推荐成立、图片链接可访问或作者结论可靠。查询旧版本不改变状态，原有引用和原始快照留存。

## 6. 验证与后续边界

无网络演示：`uv run python -m scripts.guide_import_demo`。

非数据库测试验证输入、顺序、版本、恢复、取消、发布、复制隔离和 CLI 错误。真实数据库测试文件是 `tests/test_guide_postgres.py`，使用随机 `guide_week1_<UUID>` schema，执行实际升级并保留原任务、比较迁移与 ORM、验证双 Engine 并发以及事务失败后跨 Engine 恢复。

测试退出只释放连接池，不删除 schema；不要自动执行 downgrade、DROP SCHEMA 或删除业务表来恢复失败测试。实际验证结果以当次交付记录为准，测试存在不等于已通过。

真实来源获取与 ASR、OCR 等解析仍需独立验证。检索与带引用问答见 [关键词检索](knowledge-retrieval.md) 和 [模型与回答说明](model-answering.md)。
