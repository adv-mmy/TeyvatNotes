# 2026-10-05 PostgreSQL 与Wiki词表补验

## 实际结果

用户启动本机 `postgresql-x64-18` 服务后，真实连接成功。完整测试最终为 **942 passed in 48.88s**，没有skip或deselect；其中896项非PG、46项真实PG。Ruff check通过、159个Python文件格式检查通过、strict mypy110个源文件通过。

本轮Wiki词表为 `2026-10-04.2`，444条，资源131,801字节，内容哈希：

```text
1c3eaae4df36fdbcc60fc95d1fecd5811bfcc0d9a40a03713a4bcf2b9c984c13
```

此前896 passed / 45 deselected为数据库服务未就绪时的离线历史。本次增加一个专名真实持久化用例，补完旧45项PG用例后得到942总数，不把过去的deselected当作已验收。

## 隔离目标与保留策略

私有配置当时仅提供本机 `TEST_DATABASE_URL`，原库 `agent_runtime_test` 已有 `tasks` 和 `alembic_version`，没有配置业务 `DATABASE_URL`。旧仓储测试的 `_migrate()` 会降至base并重建表，因此不能在这个现有库直接运行全套。

只读核实创建权限与目标不存在后，新建：

```text
agent_runtime_wiki_verify_20261005_23b51372b89c
```

测试进程临时将 `TEST_DATABASE_URL` 指向此新库；知识、RAG、会话和词表测试再使用 `guide_week1_<UUID>` 随机schema。真实升级到head包含0003～0006；旧迁移重建只发生在新库的测试公共schema，不能据此声称原库或业务库已升级。验证库与随机schema保留，不自动DROP或清理；连接池正常释放。

结束后只读查询确认新库public迁移版本为`20261004_0006`，保留47个随机测试schema（包括首轮中断留下的证据）；schema数量不等于测试数量。

不修改`.env`，不打印数据库密码，不触碰原测试库业务表，不修改模型配置，也不发问答模型请求。业务运行仍需使用者显式配置并核对 `DATABASE_URL`、确认迁移目标后执行升级。

## 首轮中断与重跑

首轮全套runner继承`.env`中的 `AGENT_RUNTIME_EXECUTION_MODE=api_only`，导致预期embedded行为的API用例出现3个失败，并在后续SSE等待。这个运行未完成，不计为942验收，也没有证据把它归因于数据库生产缺陷。只读检查验证库会话当时无锁等待连接。

核对本次测试Python进程的PID、路径及启动时间后只停止该进程，未停止PG服务或删除资料。重新运行前清除测试子进程中的`AGENT_RUNTIME_`、`LLM_`、`GUIDE_`、`OPENASI_`、`WORKER_`及`DATABASE_URL`，只保留隔离测试连接；未改开发者实际配置。新basetemp目录经过不存在校验，避免覆盖之前的测试证据。

新增词表测试在审查中另修正了测试输入：`article_snapshot`默认为quest，必须显式 `guide_kind="character"` 才能验证角色实体规范化。修正的是测试构造，不改变生产分类规则，也没有跳过断言。

## 本轮覆盖

旧45项真实PG覆盖任务/Worker迁移、事务、领取、租约与恢复；知识入库检查点、并发导入、发布和范围检索；RAG存储/批次原子性/跨连接；私有会话修订竞争、状态持久化，以及模拟模型/工具链。模拟协议并不代表真实服务商或Embedding已实测。

新增 `tests/test_terminology_postgres.py` 在随机schema中模拟旧空词表与新Wiki词表，验证：

- 词表内容变更产生新文档身份，旧原名证据和旧词表哈希不被覆盖。
- 角色实体及证据规范化为“奥黛塔”，原“奥萝塔”正文、实体、更正位置和normalized_text坐标保留。
- 444条完整词表和内容哈希真实存入JSONB。
- 更换独立Engine后，仍能读取审计、更正后的证据、按标准名检索与回读，并复用三个检查点。

这是数据库及确定性校对验收，不是新真实模型答案、攻略事实审核、完整标准名自动同步器或业务环境上线验收。Wiki来源及别名维护规则见 [专名维护](terminology.md)。
