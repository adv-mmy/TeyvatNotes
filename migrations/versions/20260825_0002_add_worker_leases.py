"""为 PostgreSQL Worker 增加原子领取、租约与重试字段。

Revision ID: 20260825_0002
Revises: 20260810_0001
Create Date: 2026-08-25
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260825_0002"
down_revision: str | Sequence[str] | None = "20260810_0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # 0001 在命名约定已自动添加表名前缀的情况下，又在显式名称中写入了
    # 同一前缀。这里把历史约束统一为 ORM 元数据所使用的规范名称。
    op.execute(
        "ALTER TABLE tasks RENAME CONSTRAINT "
        "ck_tasks_ck_tasks_status_allowed TO ck_tasks_status_allowed"
    )
    op.execute(
        "ALTER TABLE tasks RENAME CONSTRAINT "
        "ck_tasks_ck_tasks_task_type_allowed TO ck_tasks_task_type_allowed"
    )
    op.execute(
        "ALTER TABLE tasks RENAME CONSTRAINT "
        "ck_tasks_ck_tasks_version_non_negative TO ck_tasks_version_non_negative"
    )
    op.execute(
        "ALTER TABLE task_attempts RENAME CONSTRAINT "
        "ck_task_attempts_ck_task_attempts_attempt_number_positive "
        "TO ck_task_attempts_attempt_number_positive"
    )
    op.execute(
        "ALTER TABLE task_events RENAME CONSTRAINT "
        "ck_task_events_ck_task_events_from_status_allowed "
        "TO ck_task_events_from_status_allowed"
    )
    op.execute(
        "ALTER TABLE task_events RENAME CONSTRAINT "
        "ck_task_events_ck_task_events_to_status_allowed "
        "TO ck_task_events_to_status_allowed"
    )

    op.add_column("tasks", sa.Column("available_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column(
        "tasks",
        sa.Column("attempts_in_cycle", sa.Integer(), server_default=sa.text("0"), nullable=False),
    )
    op.add_column("tasks", sa.Column("request_id", sa.String(length=128), nullable=True))
    op.execute("UPDATE tasks SET available_at = updated_at")
    op.execute(
        """
        UPDATE tasks
        SET request_id = (
            SELECT task_events.payload ->> 'request_id'
            FROM task_events
            WHERE task_events.task_id = tasks.id
              AND task_events.event_type = 'task_created'
            ORDER BY task_events.id
            LIMIT 1
        )
        """
    )
    op.alter_column("tasks", "available_at", nullable=False)
    op.create_check_constraint(
        op.f("ck_tasks_attempts_in_cycle_non_negative"),
        "tasks",
        "attempts_in_cycle >= 0",
    )

    op.add_column("task_attempts", sa.Column("attempt_in_cycle", sa.Integer(), nullable=True))
    op.add_column("task_attempts", sa.Column("worker_id", sa.String(length=128), nullable=True))
    op.add_column(
        "task_attempts",
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column("task_attempts", sa.Column("outcome", sa.String(length=32), nullable=True))
    op.add_column(
        "task_attempts",
        sa.Column("retry_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column("task_attempts", sa.Column("request_id", sa.String(length=128), nullable=True))
    op.execute(
        """
        UPDATE task_attempts AS attempt
        SET attempt_in_cycle = attempt.attempt_number,
            worker_id = 'legacy-embedded',
            lease_expires_at = COALESCE(attempt.finished_at, attempt.started_at),
            outcome = CASE
                WHEN attempt.finished_at IS NULL THEN 'running'
                ELSE 'legacy_unknown'
            END,
            request_id = task.request_id
        FROM tasks AS task
        WHERE task.id = attempt.task_id
        """
    )
    op.execute(
        """
        WITH ranked AS (
            SELECT id,
                   row_number() OVER (
                       PARTITION BY task_id ORDER BY started_at DESC, id DESC
                   ) AS active_rank
            FROM task_attempts
            WHERE finished_at IS NULL
        )
        UPDATE task_attempts AS attempt
        SET finished_at = attempt.started_at,
            outcome = 'lease_expired',
            error = COALESCE(attempt.error, '迁移时关闭重复活动 attempt')
        FROM ranked
        WHERE ranked.id = attempt.id
          AND ranked.active_rank > 1
        """
    )
    op.execute(
        """
        UPDATE tasks AS task
        SET attempts_in_cycle = attempt.attempt_in_cycle
        FROM task_attempts AS attempt
        WHERE attempt.task_id = task.id
          AND attempt.finished_at IS NULL
        """
    )
    for column_name in ("attempt_in_cycle", "worker_id", "lease_expires_at", "outcome"):
        op.alter_column("task_attempts", column_name, nullable=False)
    op.create_check_constraint(
        op.f("ck_task_attempts_attempt_in_cycle_positive"),
        "task_attempts",
        "attempt_in_cycle >= 1",
    )
    op.create_check_constraint(
        op.f("ck_task_attempts_worker_id_non_empty"),
        "task_attempts",
        "char_length(worker_id) >= 1",
    )
    op.create_check_constraint(
        op.f("ck_task_attempts_lease_not_before_start"),
        "task_attempts",
        "lease_expires_at >= started_at",
    )
    op.create_check_constraint(
        op.f("ck_task_attempts_outcome_allowed"),
        "task_attempts",
        "outcome IN ('running', 'succeeded', 'retry_scheduled', 'retries_exhausted', "
        "'permanent_failure', 'lease_expired', 'cancelled', 'legacy_unknown')",
    )
    op.create_index(
        "ix_task_attempts_outcome_lease_expires_at",
        "task_attempts",
        ["outcome", "lease_expires_at"],
        unique=False,
    )
    op.create_index(
        "uq_task_attempts_active_task",
        "task_attempts",
        ["task_id"],
        unique=True,
        postgresql_where=sa.text("finished_at IS NULL"),
    )


def downgrade() -> None:
    op.drop_index("uq_task_attempts_active_task", table_name="task_attempts")
    op.drop_index("ix_task_attempts_outcome_lease_expires_at", table_name="task_attempts")
    op.drop_constraint(op.f("ck_task_attempts_outcome_allowed"), "task_attempts", type_="check")
    op.drop_constraint(
        op.f("ck_task_attempts_lease_not_before_start"), "task_attempts", type_="check"
    )
    op.drop_constraint(op.f("ck_task_attempts_worker_id_non_empty"), "task_attempts", type_="check")
    op.drop_constraint(
        op.f("ck_task_attempts_attempt_in_cycle_positive"),
        "task_attempts",
        type_="check",
    )
    for column_name in (
        "request_id",
        "retry_at",
        "outcome",
        "lease_expires_at",
        "worker_id",
        "attempt_in_cycle",
    ):
        op.drop_column("task_attempts", column_name)

    op.drop_constraint(op.f("ck_tasks_attempts_in_cycle_non_negative"), "tasks", type_="check")
    op.drop_column("tasks", "request_id")
    op.drop_column("tasks", "attempts_in_cycle")
    op.drop_column("tasks", "available_at")

    # downgrade 必须准确还原 0001 实际生成的约束名称。
    op.execute(
        "ALTER TABLE task_events RENAME CONSTRAINT "
        "ck_task_events_to_status_allowed TO ck_task_events_ck_task_events_to_status_allowed"
    )
    op.execute(
        "ALTER TABLE task_events RENAME CONSTRAINT "
        "ck_task_events_from_status_allowed TO ck_task_events_ck_task_events_from_status_allowed"
    )
    op.execute(
        "ALTER TABLE task_attempts RENAME CONSTRAINT "
        "ck_task_attempts_attempt_number_positive "
        "TO ck_task_attempts_ck_task_attempts_attempt_number_positive"
    )
    op.execute(
        "ALTER TABLE tasks RENAME CONSTRAINT "
        "ck_tasks_version_non_negative TO ck_tasks_ck_tasks_version_non_negative"
    )
    op.execute(
        "ALTER TABLE tasks RENAME CONSTRAINT "
        "ck_tasks_task_type_allowed TO ck_tasks_ck_tasks_task_type_allowed"
    )
    op.execute(
        "ALTER TABLE tasks RENAME CONSTRAINT "
        "ck_tasks_status_allowed TO ck_tasks_ck_tasks_status_allowed"
    )
