"""SQLAlchemy 2.x ORM 持久化模型。"""

from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Identity,
    Index,
    Integer,
    MetaData,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PostgreSQLUUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

NAMING_CONVENTION = {
    "ix": "ix_%(table_name)s_%(column_0_N_name)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_N_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}

TASK_STATUS_VALUES = "'queued', 'running', 'succeeded', 'cancel_requested', 'cancelled', 'failed'"
TASK_TYPE_VALUES = "'sum_numbers', 'word_count'"
ATTEMPT_OUTCOME_VALUES = (
    "'running', 'succeeded', 'retry_scheduled', 'retries_exhausted', "
    "'permanent_failure', 'lease_expired', 'cancelled', 'legacy_unknown'"
)


class Base(DeclarativeBase):
    """共享命名约定的 ORM 声明基类。"""

    metadata = MetaData(naming_convention=NAMING_CONVENTION)


class TaskRecord(Base):
    """tasks 表：业务任务当前状态与幂等入口。"""

    __tablename__ = "tasks"
    __table_args__ = (
        CheckConstraint(
            f"task_type IN ({TASK_TYPE_VALUES})",
            name="task_type_allowed",
        ),
        CheckConstraint(
            f"status IN ({TASK_STATUS_VALUES})",
            name="status_allowed",
        ),
        CheckConstraint("version >= 0", name="version_non_negative"),
        CheckConstraint("attempts_in_cycle >= 0", name="attempts_in_cycle_non_negative"),
        UniqueConstraint("idempotency_key", name="uq_tasks_idempotency_key"),
        Index("ix_tasks_status_created_at_id", "status", "created_at", "id"),
        Index("ix_tasks_created_at_id", "created_at", "id"),
    )

    id: Mapped[UUID] = mapped_column(PostgreSQLUUID(as_uuid=True), primary_key=True)
    task_type: Mapped[str] = mapped_column(String(32), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    result: Mapped[Any | None] = mapped_column(JSONB, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    idempotency_key: Mapped[str | None] = mapped_column(String(255), nullable=True)
    version: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    available_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    attempts_in_cycle: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        server_default=text("0"),
    )
    request_id: Mapped[str | None] = mapped_column(String(128), nullable=True)


class TaskAttemptRecord(Base):
    """task_attempts 表：每次执行 attempt 的开始、结束与错误。"""

    __tablename__ = "task_attempts"
    __table_args__ = (
        CheckConstraint("attempt_number >= 1", name="attempt_number_positive"),
        CheckConstraint("attempt_in_cycle >= 1", name="attempt_in_cycle_positive"),
        CheckConstraint("char_length(worker_id) >= 1", name="worker_id_non_empty"),
        CheckConstraint(
            "lease_expires_at >= started_at",
            name="lease_not_before_start",
        ),
        CheckConstraint(
            f"outcome IN ({ATTEMPT_OUTCOME_VALUES})",
            name="outcome_allowed",
        ),
        UniqueConstraint(
            "task_id",
            "attempt_number",
            name="uq_task_attempts_task_id_attempt_number",
        ),
        Index("ix_task_attempts_outcome_lease_expires_at", "outcome", "lease_expires_at"),
        Index(
            "uq_task_attempts_active_task",
            "task_id",
            unique=True,
            postgresql_where=text("finished_at IS NULL"),
        ),
    )

    id: Mapped[UUID] = mapped_column(PostgreSQLUUID(as_uuid=True), primary_key=True)
    task_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True),
        ForeignKey("tasks.id", ondelete="CASCADE"),
        nullable=False,
    )
    attempt_number: Mapped[int] = mapped_column(Integer, nullable=False)
    attempt_in_cycle: Mapped[int] = mapped_column(Integer, nullable=False)
    worker_id: Mapped[str] = mapped_column(String(128), nullable=False)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    lease_expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    outcome: Mapped[str] = mapped_column(String(32), nullable=False)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    retry_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    request_id: Mapped[str | None] = mapped_column(String(128), nullable=True)


class TaskEventRecord(Base):
    """task_events 表：状态变更审计事件，后续可作为 SSE 数据源。"""

    __tablename__ = "task_events"
    __table_args__ = (
        CheckConstraint(
            f"from_status IS NULL OR from_status IN ({TASK_STATUS_VALUES})",
            name="from_status_allowed",
        ),
        CheckConstraint(
            f"to_status IS NULL OR to_status IN ({TASK_STATUS_VALUES})",
            name="to_status_allowed",
        ),
        Index("ix_task_events_task_id_id", "task_id", "id"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    task_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True),
        ForeignKey("tasks.id", ondelete="CASCADE"),
        nullable=False,
    )
    event_type: Mapped[str] = mapped_column(String(64), nullable=False)
    from_status: Mapped[str | None] = mapped_column(String(32), nullable=True)
    to_status: Mapped[str | None] = mapped_column(String(32), nullable=True)
    event_payload: Mapped[dict[str, Any]] = mapped_column(
        "payload",
        JSONB,
        nullable=False,
        server_default=text("'{}'::jsonb"),
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
