"""第四周 PostgreSQL 仓储与事务集成测试。"""

import asyncio
import json
import os
from collections.abc import Iterator
from contextlib import AbstractAsyncContextManager
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import TracebackType
from typing import Any, cast
from uuid import uuid4

import pytest
from alembic import command
from alembic.config import Config
from conftest import AUTH_HEADERS
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
from agent_runtime_foundation.domain import JsonValue, Task, TaskResult, TaskStatus, TaskType
from agent_runtime_foundation.observability import bind_correlation_context
from agent_runtime_foundation.repositories import (
    PostgresTaskRepository,
    PostgresWorkerRepository,
)
from agent_runtime_foundation.runtime import DeterministicTaskExecutor
from agent_runtime_foundation.worker import (
    AttemptOutcome,
    LeaseOwnershipError,
    RetryPolicy,
    TaskWorker,
    WorkerSettings,
)
from scripts.postgres_recovery_demo import run_postgres_recovery_demo

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


def _upgrade(database_url: str, revision: str) -> None:
    """保留已有数据，仅把当前测试数据库升级到指定版本。"""

    previous_database_url = os.environ.get("DATABASE_URL")
    os.environ["DATABASE_URL"] = database_url
    try:
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


def test_worker_migration_preserves_legacy_attempt_history(
    postgres_database_url: str,
) -> None:
    task_id = uuid4()
    attempt_ids = [uuid4(), uuid4(), uuid4()]
    now = datetime.now(UTC)
    _migrate(postgres_database_url, "20260810_0001")

    async def seed_legacy_schema() -> None:
        database = create_database(postgres_database_url)
        try:
            async with database.engine.begin() as connection:
                await connection.execute(
                    text(
                        """
                        INSERT INTO tasks (
                            id, task_type, payload, status, result, error,
                            idempotency_key, version, created_at, updated_at
                        ) VALUES (
                            :id, 'sum_numbers', CAST(:payload AS jsonb), 'running',
                            NULL, NULL, NULL, 3, :created_at, :updated_at
                        )
                        """
                    ),
                    {
                        "id": task_id,
                        "payload": json.dumps({"numbers": [1, 2]}),
                        "created_at": now,
                        "updated_at": now,
                    },
                )
                await connection.execute(
                    text(
                        """
                        INSERT INTO task_attempts (
                            id, task_id, attempt_number, started_at, finished_at, error
                        ) VALUES
                            (:first, :task_id, 1, :first_started, :first_finished, '旧失败'),
                            (:second, :task_id, 2, :second_started, :second_finished, NULL),
                            (:third, :task_id, 3, :third_started, NULL, NULL)
                        """
                    ),
                    {
                        "first": attempt_ids[0],
                        "second": attempt_ids[1],
                        "third": attempt_ids[2],
                        "task_id": task_id,
                        "first_started": now - timedelta(minutes=5),
                        "first_finished": now - timedelta(minutes=4),
                        "second_started": now - timedelta(minutes=3),
                        "second_finished": now - timedelta(minutes=2),
                        "third_started": now - timedelta(minutes=1),
                    },
                )
        finally:
            await database.dispose()

    async def verify_upgraded_schema() -> None:
        database = create_database(postgres_database_url)
        try:
            async with database.session_factory() as session:
                outcomes = (
                    await session.execute(
                        text(
                            "SELECT outcome, error, attempt_in_cycle "
                            "FROM task_attempts WHERE task_id = :task_id ORDER BY attempt_number"
                        ),
                        {"task_id": task_id},
                    )
                ).all()
                attempts_in_cycle = await session.scalar(
                    text("SELECT attempts_in_cycle FROM tasks WHERE id = :task_id"),
                    {"task_id": task_id},
                )
                constraint_names = set(
                    (
                        await session.scalars(
                            text(
                                "SELECT conname FROM pg_constraint "
                                "WHERE conrelid IN ('tasks'::regclass, 'task_attempts'::regclass)"
                            )
                        )
                    ).all()
                )
        finally:
            await database.dispose()

        assert [tuple(row) for row in outcomes] == [
            (AttemptOutcome.LEGACY_UNKNOWN.value, "旧失败", 1),
            (AttemptOutcome.LEGACY_UNKNOWN.value, None, 2),
            (AttemptOutcome.RUNNING.value, None, 3),
        ]
        assert attempts_in_cycle == 3
        assert "ck_tasks_attempts_in_cycle_non_negative" in constraint_names
        assert "ck_task_attempts_outcome_allowed" in constraint_names
        assert not any("ck_tasks_ck_tasks" in name for name in constraint_names)
        assert not any("ck_task_attempts_ck_task_attempts" in name for name in constraint_names)

    try:
        asyncio.run(seed_legacy_schema())
        _upgrade(postgres_database_url, "head")
        asyncio.run(verify_upgraded_schema())
    finally:
        _migrate(postgres_database_url, "head")


def test_completion_rechecks_database_clock_after_waiting_for_lock(
    postgres_database_url: str,
) -> None:
    async def scenario() -> None:
        await _truncate_task_tables(postgres_database_url)
        database = create_database(postgres_database_url)
        tasks = PostgresTaskRepository(database.session_factory)
        workers = PostgresWorkerRepository(database.session_factory)
        try:
            creation = await tasks.add(
                Task(task_type=TaskType.SUM_NUMBERS, payload={"numbers": [1, 2]})
            )
            task = creation.task
            claim = await workers.claim_next(
                worker_id="lock-wait-worker",
                lease_duration=timedelta(seconds=1),
                now=datetime.now(UTC),
            )
            assert claim is not None
            assert claim.task.id == task.id

            async with database.session_factory.begin() as lock_session:
                await lock_session.execute(
                    select(TaskRecord).where(TaskRecord.id == task.id).with_for_update()
                )
                completion = asyncio.create_task(
                    workers.complete_claim(claim, output=3, now=datetime.now(UTC))
                )
                await asyncio.sleep(1.2)

            with pytest.raises(LeaseOwnershipError, match="租约已过期"):
                await completion
        finally:
            await database.dispose()

    asyncio.run(scenario())


def test_readiness_rejects_database_still_at_first_migration(
    postgres_database_url: str,
) -> None:
    _migrate(postgres_database_url, "20260810_0001")
    try:
        with TestClient(
            create_app(database_url=postgres_database_url, execution_mode="api_only")
        ) as client:
            response = client.get("/health/ready")
        assert response.status_code == 503
        assert response.json()["code"] == "not_ready"
    finally:
        _migrate(postgres_database_url, "head")


async def _truncate_task_tables(database_url: str) -> None:
    """为 Worker 并发场景提供不受其他集成测试干扰的空表。"""

    database = create_database(database_url)
    try:
        async with database.engine.begin() as connection:
            await connection.execute(
                text("TRUNCATE task_events, task_attempts, tasks RESTART IDENTITY CASCADE")
            )
    finally:
        await database.dispose()


class ControlledDatabaseClock:
    """让 PostgreSQL Repository 测试显式控制数据库权威时间。"""

    def __init__(self, now: datetime) -> None:
        self.now = now

    async def __call__(self, _session: AsyncSession) -> datetime:
        return self.now


def test_readiness_probe_queries_every_required_table() -> None:
    class RecordingSession(AbstractAsyncContextManager[Any]):
        def __init__(self) -> None:
            self.statements: list[str] = []

        async def __aexit__(
            self,
            exc_type: type[BaseException] | None,
            exc_value: BaseException | None,
            traceback: TracebackType | None,
        ) -> None:
            return None

        async def execute(self, statement: object) -> None:
            self.statements.append(str(statement))

    async def scenario() -> None:
        session = RecordingSession()
        repository = PostgresTaskRepository(lambda: session)  # type: ignore[arg-type]

        assert await repository.is_ready() is True
        assert len(session.statements) == 3
        assert any("FROM tasks" in statement for statement in session.statements)
        assert any("FROM task_attempts" in statement for statement in session.statements)
        assert any("FROM task_events" in statement for statement in session.statements)

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
    with TestClient(
        create_app(database_url=postgres_database_url),
        headers=AUTH_HEADERS,
    ) as first_client:
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


def test_postgres_workers_atomically_claim_one_task(
    postgres_database_url: str,
) -> None:
    async def scenario() -> None:
        await _truncate_task_tables(postgres_database_url)
        database = create_database(postgres_database_url)
        tasks = PostgresTaskRepository(database.session_factory)
        now = datetime.now(UTC) + timedelta(seconds=1)
        database_clock = ControlledDatabaseClock(now)
        first_worker = PostgresWorkerRepository(
            database.session_factory,
            clock=database_clock,
        )
        second_worker = PostgresWorkerRepository(
            database.session_factory,
            clock=database_clock,
        )
        try:
            task = Task(payload={"numbers": [1, 2, 3]})
            await tasks.add(task)
            claims = await asyncio.gather(
                first_worker.claim_next(
                    worker_id="worker-a",
                    now=now + timedelta(days=365),
                    lease_duration=timedelta(seconds=30),
                ),
                second_worker.claim_next(
                    worker_id="worker-b",
                    now=now - timedelta(days=365),
                    lease_duration=timedelta(seconds=30),
                ),
            )
            claimed = [claim for claim in claims if claim is not None]
            assert len(claimed) == 1
            assert claimed[0].task.id == task.id
            assert claimed[0].attempt_number == 1
            assert claimed[0].attempt_in_cycle == 1
            assert claimed[0].claimed_at == now

            attempts = await first_worker.list_attempts(task.id)
            assert len(attempts) == 1
            assert attempts[0].outcome is AttemptOutcome.RUNNING
            assert attempts[0].worker_id in {"worker-a", "worker-b"}
        finally:
            await database.dispose()

    asyncio.run(scenario())


def test_postgres_expired_lease_is_recovered_and_stale_worker_is_rejected(
    postgres_database_url: str,
) -> None:
    async def scenario() -> None:
        await _truncate_task_tables(postgres_database_url)
        database = create_database(postgres_database_url)
        tasks = PostgresTaskRepository(database.session_factory)
        started_at = datetime.now(UTC) + timedelta(seconds=1)
        database_clock = ControlledDatabaseClock(started_at)
        workers = PostgresWorkerRepository(database.session_factory, clock=database_clock)
        try:
            task = Task(payload={"numbers": [2, 4]})
            await tasks.add(task)
            stale_claim = await workers.claim_next(
                worker_id="worker-crashed",
                now=started_at,
                lease_duration=timedelta(seconds=5),
            )
            assert stale_claim is not None

            intruder = replace(stale_claim, worker_id="worker-intruder")
            with pytest.raises(LeaseOwnershipError, match="不拥有"):
                await workers.complete_claim(intruder, output=6, now=started_at)

            recovery_time = started_at + timedelta(seconds=6)
            database_clock.now = recovery_time
            with pytest.raises(LeaseOwnershipError, match="租约已过期"):
                await workers.complete_claim(stale_claim, output=6, now=started_at)
            assert await workers.recover_expired(now=recovery_time, max_attempts=3) == 1
            recovered_task = await tasks.get(task.id)
            assert recovered_task is not None
            assert recovered_task.status is TaskStatus.QUEUED

            with pytest.raises(LeaseOwnershipError, match="活动租约"):
                await workers.complete_claim(stale_claim, output=6, now=recovery_time)
            with pytest.raises(LeaseOwnershipError, match="活动租约"):
                await workers.fail_claim(
                    stale_claim,
                    error="迟到失败",
                    retryable=True,
                    retry_at=recovery_time + timedelta(seconds=1),
                    now=recovery_time,
                )

            replacement = await workers.claim_next(
                worker_id="worker-replacement",
                now=recovery_time,
                lease_duration=timedelta(seconds=5),
            )
            assert replacement is not None
            database_clock.now = recovery_time + timedelta(seconds=1)
            completed = await workers.complete_claim(
                replacement,
                output=6,
                now=recovery_time + timedelta(seconds=1),
            )
            assert completed.status is TaskStatus.SUCCEEDED

            attempts = await workers.list_attempts(task.id)
            assert [attempt.outcome for attempt in attempts] == [
                AttemptOutcome.LEASE_EXPIRED,
                AttemptOutcome.SUCCEEDED,
            ]
            assert [attempt.worker_id for attempt in attempts] == [
                "worker-crashed",
                "worker-replacement",
            ]
        finally:
            await database.dispose()

    asyncio.run(scenario())


def test_two_postgres_repositories_recover_one_expired_claim_once(
    postgres_database_url: str,
) -> None:
    async def scenario() -> None:
        await _truncate_task_tables(postgres_database_url)
        database = create_database(postgres_database_url)
        tasks = PostgresTaskRepository(database.session_factory)
        started_at = datetime.now(UTC) + timedelta(seconds=1)
        database_clock = ControlledDatabaseClock(started_at)
        first = PostgresWorkerRepository(database.session_factory, clock=database_clock)
        second = PostgresWorkerRepository(database.session_factory, clock=database_clock)
        try:
            task = Task(payload={"numbers": [9]})
            await tasks.add(task)
            claim = await first.claim_next(
                worker_id="worker-crashed",
                now=started_at,
                lease_duration=timedelta(seconds=5),
            )
            assert claim is not None
            database_clock.now = started_at + timedelta(seconds=6)

            recovered = await asyncio.gather(
                first.recover_expired(now=database_clock.now, max_attempts=3),
                second.recover_expired(now=database_clock.now, max_attempts=3),
            )
            assert sum(recovered) == 1
            assert sorted(recovered) == [0, 1]
            restored = await tasks.get(task.id)
            attempts = await first.list_attempts(task.id)
            assert restored is not None
            assert restored.status is TaskStatus.QUEUED
            assert [attempt.outcome for attempt in attempts] == [AttemptOutcome.LEASE_EXPIRED]
        finally:
            await database.dispose()

    asyncio.run(scenario())


def test_postgres_retry_cycle_and_request_correlation_are_persistent(
    postgres_database_url: str,
) -> None:
    async def scenario() -> None:
        await _truncate_task_tables(postgres_database_url)
        database = create_database(postgres_database_url)
        tasks = PostgresTaskRepository(database.session_factory)
        now = datetime.now(UTC) + timedelta(seconds=1)
        database_clock = ControlledDatabaseClock(now)
        workers = PostgresWorkerRepository(database.session_factory, clock=database_clock)
        try:
            task = Task(payload={"numbers": [7]}, idempotency_key=f"worker-{uuid4()}")
            with bind_correlation_context(request_id="request-worker-1", inherit=False):
                await tasks.add(task)
            first = await workers.claim_next(
                worker_id="worker-retry",
                now=now,
                lease_duration=timedelta(seconds=30),
            )
            assert first is not None
            retry_at = now + timedelta(seconds=2)
            database_clock.now = now + timedelta(seconds=1)
            queued = await workers.fail_claim(
                first,
                error="暂时失败",
                retryable=True,
                retry_at=retry_at,
                now=now + timedelta(seconds=1),
            )
            assert queued.status is TaskStatus.QUEUED
            assert (
                await workers.claim_next(
                    worker_id="too-early",
                    now=now + timedelta(seconds=1),
                    lease_duration=timedelta(seconds=30),
                )
                is None
            )

            database_clock.now = retry_at
            second = await workers.claim_next(
                worker_id="worker-retry",
                now=retry_at,
                lease_duration=timedelta(seconds=30),
            )
            assert second is not None
            assert second.attempt_number == 2
            assert second.attempt_in_cycle == 2
            database_clock.now = retry_at + timedelta(seconds=1)
            failed = await workers.fail_claim(
                second,
                error="永久失败",
                retryable=False,
                retry_at=None,
                now=retry_at + timedelta(seconds=1),
            )
            assert failed.status is TaskStatus.FAILED

            manually_retried = await tasks.transition(task.id, TaskStatus.QUEUED)
            assert manually_retried is not None
            database_clock.now = datetime.now(UTC) + timedelta(seconds=1)
            third = await workers.claim_next(
                worker_id="worker-new-cycle",
                now=database_clock.now,
                lease_duration=timedelta(seconds=30),
            )
            assert third is not None
            assert third.attempt_number == 3
            assert third.attempt_in_cycle == 1
            assert third.request_id == "request-worker-1"

            events = await tasks.list_events(task.id, after_id=0, limit=100)
            assert events is not None
            worker_events = [event for event in events if "worker_id" in event.payload]
            assert worker_events
            assert all(event.payload.get("request_id") == "request-worker-1" for event in events)
            assert {event.payload["worker_id"] for event in worker_events} >= {
                "worker-retry",
                "worker-new-cycle",
            }
        finally:
            await database.dispose()

    asyncio.run(scenario())


def test_real_task_worker_executes_through_postgres_repository(
    postgres_database_url: str,
) -> None:
    async def scenario() -> None:
        await _truncate_task_tables(postgres_database_url)
        database = create_database(postgres_database_url)
        tasks = PostgresTaskRepository(database.session_factory)
        workers = PostgresWorkerRepository(database.session_factory)
        try:
            task = Task(payload={"numbers": [3, 4, 5]})
            await tasks.add(task)
            worker = TaskWorker(
                workers,
                DeterministicTaskExecutor(),
                WorkerSettings(
                    worker_id="integration-worker",
                    lease_duration_seconds=5,
                    task_timeout_seconds=1,
                    retry_policy=RetryPolicy(max_attempts=2),
                ),
            )
            assert await worker.run_once() is True
            completed = await tasks.get(task.id)
            assert completed is not None
            assert completed.status is TaskStatus.SUCCEEDED
            assert completed.result is not None
            assert completed.result.output == {"total": 12}
        finally:
            await database.dispose()

    asyncio.run(scenario())


def test_postgres_recovery_demo_uses_new_engine_and_replacement_worker(
    postgres_database_url: str,
) -> None:
    report = asyncio.run(run_postgres_recovery_demo(postgres_database_url))

    assert report.status_before_recovery == "running"
    assert report.status_after_recovery == "succeeded"
    assert report.attempt_outcomes == (
        AttemptOutcome.LEASE_EXPIRED.value,
        AttemptOutcome.SUCCEEDED.value,
    )
    assert report.worker_ids == (
        "postgres-worker-crashed",
        "postgres-worker-replacement",
    )
    assert report.result == 60
    assert set(report.event_worker_ids) == set(report.worker_ids)


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
        assert counts_before == (1, 1, 3)
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
        assert counts == (1, 0, 5)

    asyncio.run(scenario())


def test_list_tasks_returns_filtered_stable_page(postgres_database_url: str) -> None:
    async def scenario() -> None:
        await _truncate_task_tables(postgres_database_url)
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
