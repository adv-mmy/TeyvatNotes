"""第四周 PostgreSQL 仓储与事务集成测试。"""

import asyncio
import json
import os
from collections.abc import Iterator
from pathlib import Path
from typing import cast
from uuid import uuid4

import pytest
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy import func, select, text
from sqlalchemy.exc import StatementError
from sqlalchemy.ext.asyncio import AsyncSession

from agent_runtime_foundation.api import create_app
from agent_runtime_foundation.database import (
    TaskAttemptRecord,
    TaskEventRecord,
    TaskRecord,
    create_database,
)
from agent_runtime_foundation.domain import JsonValue, Task, TaskResult, TaskStatus
from agent_runtime_foundation.repositories import PostgresTaskRepository

PROJECT_ROOT = Path(__file__).resolve().parents[1]
pytestmark = pytest.mark.postgres


def _migrate(database_url: str, revision: str) -> None:
    """让 Alembic 使用指定的集成测试数据库。"""

    previous_database_url = os.environ.get("DATABASE_URL")
    os.environ["DATABASE_URL"] = database_url
    try:
        command.downgrade(Config(PROJECT_ROOT / "alembic.ini"), "base")
        command.upgrade(Config(PROJECT_ROOT / "alembic.ini"), revision)
    finally:
        if previous_database_url is None:
            os.environ.pop("DATABASE_URL", None)
        else:
            os.environ["DATABASE_URL"] = previous_database_url


@pytest.fixture(scope="session")
def postgres_database_url() -> Iterator[str]:
    """把可丢弃数据库重建到 head，没有配置时跳过真实库测试。"""

    database_url = os.getenv("TEST_DATABASE_URL")
    if database_url is None:
        pytest.skip("未设置 TEST_DATABASE_URL，跳过 PostgreSQL 集成测试")

    _migrate(database_url, "head")
    yield database_url


def test_migration_creates_required_tables(postgres_database_url: str) -> None:
    async def scenario() -> None:
        database = create_database(postgres_database_url)
        try:
            async with database.session_factory() as session:
                table_names = {
                    await session.scalar(text("SELECT to_regclass('tasks')")),
                    await session.scalar(text("SELECT to_regclass('task_attempts')")),
                    await session.scalar(text("SELECT to_regclass('task_events')")),
                }
            assert table_names == {"tasks", "task_attempts", "task_events"}
        finally:
            await database.dispose()

    asyncio.run(scenario())


def test_task_survives_database_engine_restart(postgres_database_url: str) -> None:
    async def scenario() -> None:
        task = Task(payload={"numbers": [1, 2, 3]})
        first_database = create_database(postgres_database_url)
        try:
            first_repository = PostgresTaskRepository(first_database.session_factory)
            await first_repository.add(task)
        finally:
            await first_database.dispose()

        second_database = create_database(postgres_database_url)
        try:
            second_repository = PostgresTaskRepository(second_database.session_factory)
            restored = await second_repository.get(task.id)
        finally:
            await second_database.dispose()

        assert restored is not None
        assert restored.id == task.id
        assert restored.payload == task.payload
        assert restored.status is TaskStatus.QUEUED

    asyncio.run(scenario())


def test_task_remains_queryable_after_application_restart(
    postgres_database_url: str,
) -> None:
    idempotency_key = f"restart-{uuid4()}"
    with TestClient(create_app(database_url=postgres_database_url)) as first_client:
        created_response = first_client.post(
            "/v1/tasks",
            headers={"Idempotency-Key": idempotency_key},
            json={
                "task_type": "sum_numbers",
                "payload": {"numbers": [1, 2, 3]},
            },
        )
    assert created_response.status_code == 201
    task_id = created_response.json()["id"]

    with TestClient(create_app(database_url=postgres_database_url)) as second_client:
        restored_response = second_client.get(f"/v1/tasks/{task_id}")

    assert restored_response.status_code == 200
    assert restored_response.json()["id"] == task_id
    assert restored_response.json()["idempotency_key"] == idempotency_key


def test_concurrent_idempotency_key_creates_only_one_task(
    postgres_database_url: str,
) -> None:
    async def scenario() -> None:
        database = create_database(postgres_database_url)
        idempotency_key = f"week4-{uuid4()}"
        repository = PostgresTaskRepository(database.session_factory)
        try:
            first, second = await asyncio.gather(
                repository.add(
                    Task(
                        payload={"numbers": [1]},
                        idempotency_key=idempotency_key,
                    )
                ),
                repository.add(
                    Task(
                        payload={"numbers": [2]},
                        idempotency_key=idempotency_key,
                    )
                ),
            )
            async with database.session_factory() as session:
                count = await session.scalar(
                    select(func.count())
                    .select_from(TaskRecord)
                    .where(TaskRecord.idempotency_key == idempotency_key)
                )
        finally:
            await database.dispose()

        assert first.task.id == second.task.id
        assert {first.created, second.created} == {False, True}
        assert count == 1

    asyncio.run(scenario())


async def _attempt_and_event_counts(
    session: AsyncSession,
    task_id: object,
) -> tuple[int, int, int]:
    attempt_count = await session.scalar(
        select(func.count())
        .select_from(TaskAttemptRecord)
        .where(TaskAttemptRecord.task_id == task_id)
    )
    unfinished_count = await session.scalar(
        select(func.count())
        .select_from(TaskAttemptRecord)
        .where(
            TaskAttemptRecord.task_id == task_id,
            TaskAttemptRecord.finished_at.is_(None),
        )
    )
    event_count = await session.scalar(
        select(func.count()).select_from(TaskEventRecord).where(TaskEventRecord.task_id == task_id)
    )
    return int(attempt_count or 0), int(unfinished_count or 0), int(event_count or 0)


def test_failed_transaction_rolls_back_task_attempt_and_event(
    postgres_database_url: str,
) -> None:
    async def scenario() -> None:
        database = create_database(postgres_database_url)
        repository = PostgresTaskRepository(database.session_factory)
        task = Task(payload={"numbers": [1]})
        attempt_id = uuid4()
        try:
            await repository.add(task)
            await repository.transition(
                task.id,
                TaskStatus.RUNNING,
                attempt_id=attempt_id,
            )
            async with database.session_factory() as session:
                counts_before = await _attempt_and_event_counts(session, task.id)

            invalid_json = cast(JsonValue, object())
            with pytest.raises(StatementError):
                await repository.transition(
                    task.id,
                    TaskStatus.SUCCEEDED,
                    result=TaskResult(output=invalid_json),
                    attempt_id=attempt_id,
                )

            restored = await repository.get(task.id)
            async with database.session_factory() as session:
                counts_after = await _attempt_and_event_counts(session, task.id)
        finally:
            await database.dispose()

        assert restored is not None
        assert restored.status is TaskStatus.RUNNING
        assert restored.version == 1
        assert counts_before == (1, 1, 2)
        assert counts_after == counts_before

    asyncio.run(scenario())


def test_transition_commits_result_attempt_and_events(
    postgres_database_url: str,
) -> None:
    async def scenario() -> None:
        database = create_database(postgres_database_url)
        repository = PostgresTaskRepository(database.session_factory)
        task = Task(payload={"numbers": [1, 2]})
        attempt_id = uuid4()
        try:
            await repository.add(task)
            await repository.transition(
                task.id,
                TaskStatus.RUNNING,
                attempt_id=attempt_id,
            )
            succeeded = await repository.transition(
                task.id,
                TaskStatus.SUCCEEDED,
                result=TaskResult(output=3),
                attempt_id=attempt_id,
            )
            async with database.session_factory() as session:
                counts = await _attempt_and_event_counts(session, task.id)
        finally:
            await database.dispose()

        assert succeeded is not None
        assert succeeded.result == TaskResult(output=3)
        assert succeeded.version == 2
        assert counts == (1, 0, 3)

    asyncio.run(scenario())


def test_list_tasks_returns_filtered_stable_page(postgres_database_url: str) -> None:
    async def scenario() -> None:
        database = create_database(postgres_database_url)
        repository = PostgresTaskRepository(database.session_factory)
        tasks = [Task(payload={"numbers": [number]}) for number in range(3)]
        try:
            for task in tasks:
                await repository.add(task)
            await repository.transition(
                tasks[1].id,
                TaskStatus.RUNNING,
                attempt_id=uuid4(),
            )

            first_page, first_total = await repository.list_tasks(
                status=TaskStatus.QUEUED,
                offset=0,
                limit=1,
            )
            second_page, second_total = await repository.list_tasks(
                status=TaskStatus.QUEUED,
                offset=1,
                limit=1,
            )
        finally:
            await database.dispose()

        expected = sorted(
            (tasks[0], tasks[2]),
            key=lambda task: (task.created_at, task.id),
        )
        assert [task.id for task in first_page] == [expected[0].id]
        assert [task.id for task in second_page] == [expected[1].id]
        assert first_total == second_total == 2

    asyncio.run(scenario())


def test_explain_list_tasks_returns_postgresql_plan(postgres_database_url: str) -> None:
    async def scenario() -> JsonValue:
        database = create_database(postgres_database_url)
        try:
            repository = PostgresTaskRepository(database.session_factory)
            return await repository.explain_list_tasks(TaskStatus.QUEUED)
        finally:
            await database.dispose()

    plan = asyncio.run(scenario())

    assert "Plan" in json.dumps(plan)
