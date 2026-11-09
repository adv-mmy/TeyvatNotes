"""Alembic 异步迁移环境。"""

import asyncio
import os
from logging.config import fileConfig

from alembic import context
from sqlalchemy import pool, text
from sqlalchemy.engine import Connection, make_url
from sqlalchemy.exc import ArgumentError
from sqlalchemy.ext.asyncio import create_async_engine

from teyvatnotes.database import Base
from teyvatnotes.database.session import normalize_database_url

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def _database_url() -> str:
    configured = os.getenv("GUIDE_DATABASE_URL")
    if not configured:
        raise RuntimeError("必须显式配置新攻略库 GUIDE_DATABASE_URL")
    normalized = normalize_database_url(configured)
    try:
        url = make_url(normalized)
    except ArgumentError:
        raise RuntimeError("GUIDE_DATABASE_URL 无效") from None
    if url.drivername != "postgresql+asyncpg" or not url.database:
        raise RuntimeError("GUIDE_DATABASE_URL 必须指向明确的 PostgreSQL 数据库")
    return normalized


def run_migrations_offline() -> None:
    """不创建 Engine，只根据 URL 生成 SQL。"""

    context.configure(
        url=_database_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection: Connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        compare_type=True,
    )
    with context.begin_transaction():
        old_tables = connection.scalar(
            text(
                "SELECT EXISTS (SELECT 1 FROM pg_catalog.pg_tables "
                "WHERE schemaname = current_schema() AND tablename IN "
                "('tasks', 'task_attempts', 'task_events', "
                "'guide_conversations', 'guide_chunk_vectors'))"
            )
        )
        if old_tables:
            raise RuntimeError("目标含已退休的表，请使用独立的新攻略数据库")
        context.run_migrations()


async def run_async_migrations() -> None:
    connectable = create_async_engine(_database_url(), poolclass=pool.NullPool)
    try:
        async with connectable.begin() as connection:
            await connection.run_sync(do_run_migrations)
    finally:
        await connectable.dispose()


def run_migrations_online() -> None:
    """使用 asyncpg Engine 执行在线迁移。"""

    provided_connection = config.attributes.get("connection")
    if provided_connection is not None:
        if not isinstance(provided_connection, Connection):
            raise TypeError("connection 必须是 SQLAlchemy Connection")
        do_run_migrations(provided_connection)
        return
    asyncio.run(run_async_migrations())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
