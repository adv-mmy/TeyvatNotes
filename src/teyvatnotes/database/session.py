"""PostgreSQL 异步 Engine、连接池与 Session 工厂。"""

from dataclasses import dataclass

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)


def normalize_database_url(database_url: str) -> str:
    """把常见 PostgreSQL URL 规范为 asyncpg 方言 URL。"""

    if database_url.startswith("postgres://"):
        return database_url.replace("postgres://", "postgresql+asyncpg://", 1)
    if database_url.startswith("postgresql://"):
        return database_url.replace("postgresql://", "postgresql+asyncpg://", 1)
    return database_url


@dataclass(frozen=True, slots=True)
class Database:
    """共享异步 Engine 和“每事务一个 Session”的工厂。"""

    engine: AsyncEngine
    session_factory: async_sessionmaker[AsyncSession]

    async def dispose(self) -> None:
        """关闭连接池中的连接。"""

        await self.engine.dispose()


def create_database(
    database_url: str,
    *,
    pool_size: int = 5,
    max_overflow: int = 10,
) -> Database:
    """创建启用 pre-ping 的 PostgreSQL 异步连接池。"""

    if pool_size < 1:
        raise ValueError("pool_size 必须大于 0")
    if max_overflow < 0:
        raise ValueError("max_overflow 不能为负数")

    engine = create_async_engine(
        normalize_database_url(database_url),
        pool_pre_ping=True,
        pool_size=pool_size,
        max_overflow=max_overflow,
    )
    return Database(
        engine=engine,
        session_factory=async_sessionmaker(engine, expire_on_commit=False),
    )
