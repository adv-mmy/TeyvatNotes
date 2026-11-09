"""匿名访问与 UTC 日额度；服务端不存放令牌明文或原始 IP。"""

from datetime import date, datetime

from sqlalchemy import CheckConstraint, Date, DateTime, Index, Integer, String, text
from sqlalchemy.orm import Mapped, mapped_column

from teyvatnotes.database.base import Base


class GuideSessionRecord(Base):
    """仅以 SHA-256 哈希识别匿名令牌。"""

    __tablename__ = "guide_access_sessions"
    __table_args__ = (
        CheckConstraint("token_hash ~ '^[0-9a-f]{64}$'", name="token_hash_valid"),
        Index("ix_guide_access_sessions_expires_at", "expires_at"),
    )

    token_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class GuideQuotaRecord(Base):
    """在单一短事务中对各作用域的计数扣额。"""

    __tablename__ = "guide_daily_quotas"
    __table_args__ = (CheckConstraint("used >= 0", name="used_non_negative"),)

    day: Mapped[date] = mapped_column(Date(), primary_key=True)
    bucket: Mapped[str] = mapped_column(String(16), primary_key=True)
    subject: Mapped[str] = mapped_column(String(64), primary_key=True)
    used: Mapped[int] = mapped_column(Integer(), nullable=False, server_default=text("0"))
