"""数据库元数据、连接配置和 Alembic 离线迁移测试。"""

from io import StringIO
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config

from agent_runtime_foundation.database import Base, create_database, normalize_database_url

PROJECT_ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        (
            "postgres://user:pass@localhost/runtime",
            "postgresql+asyncpg://user:pass@localhost/runtime",
        ),
        (
            "postgresql://user:pass@localhost/runtime",
            "postgresql+asyncpg://user:pass@localhost/runtime",
        ),
        (
            "postgresql+asyncpg://user:pass@localhost/runtime",
            "postgresql+asyncpg://user:pass@localhost/runtime",
        ),
    ],
)
def test_normalize_database_url(source: str, expected: str) -> None:
    assert normalize_database_url(source) == expected


def test_create_database_validates_pool_settings() -> None:
    url = "postgresql+asyncpg://postgres:postgres@localhost/runtime"
    with pytest.raises(ValueError, match="pool_size"):
        create_database(url, pool_size=0)
    with pytest.raises(ValueError, match="max_overflow"):
        create_database(url, max_overflow=-1)


def test_metadata_contains_required_tables_constraints_and_indexes() -> None:
    assert set(Base.metadata.tables) == {"tasks", "task_attempts", "task_events"}

    tasks = Base.metadata.tables["tasks"]
    assert {constraint.name for constraint in tasks.constraints} >= {
        "pk_tasks",
        "uq_tasks_idempotency_key",
        "ck_tasks_task_type_allowed",
        "ck_tasks_status_allowed",
        "ck_tasks_version_non_negative",
        "ck_tasks_attempts_in_cycle_non_negative",
    }
    assert {index.name for index in tasks.indexes} == {
        "ix_tasks_created_at_id",
        "ix_tasks_status_created_at_id",
    }
    attempts = Base.metadata.tables["task_attempts"]
    assert {index.name for index in attempts.indexes} == {
        "ix_task_attempts_outcome_lease_expires_at",
        "uq_task_attempts_active_task",
    }
    assert {constraint.name for constraint in attempts.constraints} >= {
        "ck_task_attempts_attempt_in_cycle_positive",
        "ck_task_attempts_worker_id_non_empty",
        "ck_task_attempts_lease_not_before_start",
        "ck_task_attempts_outcome_allowed",
    }


def test_alembic_head_generates_three_tables_from_empty_schema() -> None:
    output = StringIO()
    configuration = Config(PROJECT_ROOT / "alembic.ini", output_buffer=output)

    command.upgrade(configuration, "head", sql=True)

    migration_sql = output.getvalue()
    assert "CREATE TABLE tasks" in migration_sql
    assert "CREATE TABLE task_attempts" in migration_sql
    assert "CREATE TABLE task_events" in migration_sql
    assert "uq_tasks_idempotency_key" in migration_sql
    assert "ix_tasks_status_created_at_id" in migration_sql
    assert "available_at" in migration_sql
    assert "lease_expires_at" in migration_sql
    assert "uq_task_attempts_active_task" in migration_sql
    assert "ix_task_attempts_task_id_attempt_number" not in migration_sql
