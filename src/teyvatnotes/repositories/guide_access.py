"""匿名访问令牌与 PostgreSQL 原子日额度。"""

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from secrets import token_urlsafe
from typing import Protocol

from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from teyvatnotes.database.access import GuideQuotaRecord, GuideSessionRecord

_GLOBAL = "0" * 64


class QuotaExceeded(Exception):
    """所有额度更新在同一个事务；任一桶不足则全部回滚。"""

    def __init__(self, retry_after: int) -> None:
        self.retry_after = retry_after


@dataclass(frozen=True, slots=True)
class SessionToken:
    access_token: str
    expires_at: datetime


class GuideAccess(Protocol):
    async def issue(self, ip_key: str, now: datetime) -> SessionToken: ...

    async def authenticate(self, token: str, now: datetime) -> str | None: ...

    async def reserve(self, principal: str, ip_key: str, now: datetime) -> None: ...


def _digest(token: str) -> str:
    return sha256(token.encode("ascii")).hexdigest()


async def _increment(
    session: AsyncSession, now: datetime, bucket: str, subject: str, limit: int
) -> None:
    row = GuideQuotaRecord
    stmt = insert(row).values(day=now.date(), bucket=bucket, subject=subject, used=1)
    returning = stmt.on_conflict_do_update(
        index_elements=[row.day, row.bucket, row.subject],
        set_={"used": row.used + 1},
        where=row.used < limit,
    ).returning(row.used)
    if await session.scalar(returning) is None:
        midnight = datetime.combine(now.date() + timedelta(days=1), datetime.min.time(), tzinfo=UTC)
        raise QuotaExceeded(max(1, int((midnight - now).total_seconds()) + 1))


class PostgresGuideAccess:
    """数据库仅保存令牌哈希和经过服务端散列的网络标识。"""

    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        self._sessions = sessions

    async def issue(self, ip_key: str, now: datetime) -> SessionToken:
        now = now.astimezone(UTC)
        token = token_urlsafe(32)
        expires = now + timedelta(days=7)
        async with self._sessions.begin() as session:
            # 全局签发保护限制任意换 IP 创建记录；IP 限制仍为每天 3 个。
            await _increment(session, now, "issue_global", _GLOBAL, 1000)
            await _increment(session, now, "issue_ip", ip_key, 3)
            session.add(GuideSessionRecord(token_hash=_digest(token), expires_at=expires))
        return SessionToken(token, expires)

    async def authenticate(self, token: str, now: datetime) -> str | None:
        digest = _digest(token)
        async with self._sessions() as session:
            row = await session.get(GuideSessionRecord, digest)
            return digest if row is not None and row.expires_at > now else None

    async def reserve(self, principal: str, ip_key: str, now: datetime) -> None:
        now = now.astimezone(UTC)
        async with self._sessions.begin() as session:
            # 固定锁顺序；不跨越模型网络请求。
            await _increment(session, now, "model_global", _GLOBAL, 100)
            await _increment(session, now, "model_ip", ip_key, 20)
            await _increment(session, now, "model_token", principal, 5)
