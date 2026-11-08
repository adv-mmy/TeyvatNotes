"""私有会话表；与任务状态、公开读接口和知识发布状态分开。"""

from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import Boolean, CheckConstraint, DateTime, Index, Integer
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PostgreSQLUUID
from sqlalchemy.orm import Mapped, mapped_column

from teyvatnotes.database.models import Base


class GuideConversationRecord(Base):
    __tablename__ = "guide_conversations"
    __table_args__ = (
        CheckConstraint("revision >= 0", name="revision_non_negative"),
        CheckConstraint("updated_at >= created_at", name="timestamps_ordered"),
        CheckConstraint(
            "CASE WHEN jsonb_typeof(payload) = 'object' AND "
            "jsonb_typeof(payload->'turns') = 'array' THEN "
            "jsonb_array_length(payload->'turns') <= 20 ELSE false END",
            name="payload_structure",
        ),
        CheckConstraint("octet_length(payload::text) <= 2000000", name="history_bounded"),
        Index("ix_guide_conversations_owner_id", "owner_id"),
    )

    id: Mapped[UUID] = mapped_column(PostgreSQLUUID(as_uuid=True), primary_key=True)
    owner_id: Mapped[UUID] = mapped_column(PostgreSQLUUID(as_uuid=True), nullable=False)
    revision: Mapped[int] = mapped_column(Integer, nullable=False)
    closed: Mapped[bool] = mapped_column(Boolean, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
