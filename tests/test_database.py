"""数据库元数据、连接配置和 Alembic 离线迁移测试。"""

from io import StringIO
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import CheckConstraint
from sqlalchemy.dialects import postgresql
from sqlalchemy.sql import and_

from teyvatnotes.database import Base, create_database, normalize_database_url
from teyvatnotes.domain.guides import GuideKind
from teyvatnotes.domain.knowledge import EvidenceScope
from teyvatnotes.repositories.knowledge_postgres import _version_filters

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
    assert set(Base.metadata.tables) == {
        "tasks",
        "task_attempts",
        "task_events",
        "guide_sources",
        "guide_documents",
        "guide_chunks",
        "guide_ingestion_steps",
        "guide_chunk_vectors",
        "guide_conversations",
    }

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
    conversations = Base.metadata.tables["guide_conversations"]
    assert {index.name for index in conversations.indexes} == {"ix_guide_conversations_owner_id"}
    assert {constraint.name for constraint in conversations.constraints} >= {
        "ck_guide_conversations_revision_non_negative",
        "ck_guide_conversations_timestamps_ordered",
        "ck_guide_conversations_payload_structure",
        "ck_guide_conversations_history_bounded",
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
    for table in ("guide_sources", "guide_documents", "guide_chunks", "guide_ingestion_steps"):
        assert f"CREATE TABLE {table}" in migration_sql
    assert "ck_guide_documents_published_metadata_valid" in migration_sql
    assert "uq_guide_documents_version" in migration_sql
    assert "CREATE TABLE guide_conversations" in migration_sql
    assert "jsonb_array_length(payload->'turns') <= 20" in migration_sql
    assert "octet_length(payload::text) <= 2000000" in migration_sql
    assert "ix_guide_conversations_owner_id" in migration_sql


def test_preview_policy_migration_and_metadata_keep_other_publication_guards() -> None:
    output = StringIO()
    configuration = Config(PROJECT_ROOT / "alembic.ini", output_buffer=output)
    command.upgrade(configuration, "20261004_0005:head", sql=True)
    migration_sql = output.getvalue()
    assert "DROP CONSTRAINT ck_guide_documents_published_metadata_valid" in migration_sql
    assert "ADD CONSTRAINT ck_guide_documents_published_metadata_valid CHECK" in migration_sql
    assert "content_nature" not in migration_sql
    assert "DELETE" not in migration_sql and "UPDATE guide_documents" not in migration_sql
    table = Base.metadata.tables["guide_documents"]
    constraint = next(
        item
        for item in table.constraints
        if isinstance(item, CheckConstraint)
        and item.name == "ck_guide_documents_published_metadata_valid"
    )
    for expression in (migration_sql, str(constraint.sqltext)):
        assert "game_version IS NOT NULL" in expression
        assert "snapshot->>'is_fixture' = 'false'" in expression
        assert "snapshot->>'complete' = 'true'" in expression
    assert "content_nature" not in str(constraint.sqltext)
    for demo in (False, True):
        filters = and_(
            *_version_filters(EvidenceScope("7.1", GuideKind.CHARACTER, "测试角色", demo_only=demo))
        )
        compiled = str(
            filters.compile(dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True})
        )
        assert "content_nature" not in compiled
        assert "game_version" in compiled and "complete" in compiled and "is_fixture" in compiled


def test_preview_policy_downgrade_restores_old_check_without_rewriting_sources() -> None:
    output = StringIO()
    command.downgrade(
        Config(PROJECT_ROOT / "alembic.ini", output_buffer=output),
        "20261004_0006:20261004_0005",
        sql=True,
    )
    migration_sql = output.getvalue()
    assert "snapshot->>'content_nature' = 'released'" in migration_sql
    assert "UPDATE guide_documents" not in migration_sql and "DELETE" not in migration_sql
