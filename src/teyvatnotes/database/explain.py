"""输出任务列表查询的 PostgreSQL EXPLAIN 计划。"""

import asyncio
import json
import os

from teyvatnotes.database.session import create_database
from teyvatnotes.repositories.postgres import PostgresTaskRepository


async def explain_tasks() -> None:
    """连接 DATABASE_URL，执行常用列表查询的 JSON EXPLAIN。"""

    database_url = os.getenv("DATABASE_URL")
    if database_url is None:
        raise RuntimeError("请先设置 DATABASE_URL")

    database = create_database(database_url)
    try:
        repository = PostgresTaskRepository(database.session_factory)
        plan = await repository.explain_list_tasks()
        print(json.dumps(plan, ensure_ascii=False, indent=2))
    finally:
        await database.dispose()


def main() -> None:
    """在独立事件循环中运行 EXPLAIN。"""

    asyncio.run(explain_tasks())


if __name__ == "__main__":
    main()
