"""创建任务、执行 attempt 和事件表。

Revision ID: 20260810_0001
Revises:
Create Date: 2026-08-10
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20260810_0001"
down_revision: str | Sequence[str] | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "tasks",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("task_type", sa.String(length=32), nullable=False),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("result", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("idempotency_key", sa.String(length=255), nullable=True),
        sa.Column("version", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "status IN ('queued', 'running', 'succeeded', 'cancel_requested', "
            "'cancelled', 'failed')",
            name="ck_tasks_status_allowed",
        ),
        sa.CheckConstraint(
            "task_type IN ('sum_numbers', 'word_count')",
            name="ck_tasks_task_type_allowed",
        ),
        sa.CheckConstraint("version >= 0", name="ck_tasks_version_non_negative"),
        sa.PrimaryKeyConstraint("id", name="pk_tasks"),
        sa.UniqueConstraint("idempotency_key", name="uq_tasks_idempotency_key"),
    )
    op.create_index(
        "ix_tasks_created_at_id",
        "tasks",
        ["created_at", "id"],
        unique=False,
    )
    op.create_index(
        "ix_tasks_status_created_at_id",
        "tasks",
        ["status", "created_at", "id"],
        unique=False,
    )

    op.create_table(
        "task_attempts",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("task_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("attempt_number", sa.Integer(), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.CheckConstraint(
            "attempt_number >= 1",
            name="ck_task_attempts_attempt_number_positive",
        ),
        sa.ForeignKeyConstraint(
            ["task_id"],
            ["tasks.id"],
            name="fk_task_attempts_task_id_tasks",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_task_attempts"),
        sa.UniqueConstraint(
            "task_id",
            "attempt_number",
            name="uq_task_attempts_task_id_attempt_number",
        ),
    )
    op.create_table(
        "task_events",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column("task_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("event_type", sa.String(length=64), nullable=False),
        sa.Column("from_status", sa.String(length=32), nullable=True),
        sa.Column("to_status", sa.String(length=32), nullable=True),
        sa.Column(
            "payload",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "from_status IS NULL OR from_status IN ('queued', 'running', 'succeeded', "
            "'cancel_requested', 'cancelled', 'failed')",
            name="ck_task_events_from_status_allowed",
        ),
        sa.CheckConstraint(
            "to_status IS NULL OR to_status IN ('queued', 'running', 'succeeded', "
            "'cancel_requested', 'cancelled', 'failed')",
            name="ck_task_events_to_status_allowed",
        ),
        sa.ForeignKeyConstraint(
            ["task_id"],
            ["tasks.id"],
            name="fk_task_events_task_id_tasks",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_task_events"),
    )
    op.create_index(
        "ix_task_events_task_id_id",
        "task_events",
        ["task_id", "id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_task_events_task_id_id", table_name="task_events")
    op.drop_table("task_events")
    op.drop_table("task_attempts")
    op.drop_index("ix_tasks_status_created_at_id", table_name="tasks")
    op.drop_index("ix_tasks_created_at_id", table_name="tasks")
    op.drop_table("tasks")
