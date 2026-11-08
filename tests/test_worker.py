"""第五周 Worker、重试、租约和崩溃恢复验收测试。"""

import asyncio
import io
import json
import logging
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from threading import Barrier, Lock
from uuid import UUID

import pytest
from conftest import AUTH_HEADERS
from fastapi import FastAPI
from fastapi.testclient import TestClient

import teyvatnotes.api as api_package
from teyvatnotes.api import create_app
from teyvatnotes.application import TaskService
from teyvatnotes.domain import JsonValue, Task, TaskStatus
from teyvatnotes.observability import JsonLogFormatter, MetricsRegistry
from teyvatnotes.repositories import InMemoryTaskRepository
from teyvatnotes.worker import (
    AttemptOutcome,
    LeaseOwnershipError,
    PermanentTaskError,
    RetryableTaskError,
    RetryPolicy,
    TaskWorker,
    WorkClaim,
    WorkerSettings,
    calculate_retry_delay,
)


@dataclass(slots=True)
class MutableClock:
    """由测试显式推进的时钟，避免依赖 sleep。"""

    current: datetime

    def __call__(self) -> datetime:
        return self.current

    def advance(self, seconds: float) -> None:
        self.current += timedelta(seconds=seconds)


class RetryTwiceExecutor:
    """前两次短暂失败，第三次成功。"""

    def __init__(self) -> None:
        self.calls = 0

    async def execute(self, task: Task) -> JsonValue:
        self.calls += 1
        if self.calls <= 2:
            raise RetryableTaskError("上游服务暂时不可用")
        return {"task_id": str(task.id), "attempts": self.calls}


class AlwaysRetryableExecutor:
    """每次都是可重试错误，用于验证最大尝试次数。"""

    def __init__(self) -> None:
        self.calls = 0

    async def execute(self, task: Task) -> JsonValue:
        del task
        self.calls += 1
        raise RetryableTaskError("临时故障")


class PermanentFailureExecutor:
    """永久失败执行器。"""

    def __init__(self) -> None:
        self.calls = 0

    async def execute(self, task: Task) -> JsonValue:
        del task
        self.calls += 1
        raise PermanentTaskError("任务参数无法执行")


class SuccessfulExecutor:
    """记录调用次数的成功执行器。"""

    def __init__(self) -> None:
        self.calls = 0

    async def execute(self, task: Task) -> JsonValue:
        self.calls += 1
        return {"task_id": str(task.id)}


class ThreadSafeSuccessfulExecutor:
    """可从多个线程安全观察副作用次数的成功执行器。"""

    def __init__(self) -> None:
        self.calls = 0
        self._lock = Lock()

    async def execute(self, task: Task) -> JsonValue:
        with self._lock:
            self.calls += 1
        return {"task_id": str(task.id)}


class TimeoutThenSuccessExecutor:
    """第一次等待超时，第二次成功。"""

    def __init__(self) -> None:
        self.calls = 0

    async def execute(self, task: Task) -> JsonValue:
        self.calls += 1
        if self.calls == 1:
            await asyncio.Event().wait()
        return {"task_id": str(task.id)}


class CancellationBlockingExecutor:
    """确认进入执行后阻塞，供测试强制中断 Worker。"""

    def __init__(self) -> None:
        self.started = asyncio.Event()

    async def execute(self, task: Task) -> JsonValue:
        del task
        self.started.set()
        await asyncio.Event().wait()
        return None


class ClaimFailsOnceRepository(InMemoryTaskRepository):
    """第一次领取模拟存储短暂不可用。"""

    def __init__(self) -> None:
        super().__init__()
        self.claim_calls = 0

    async def claim_next(
        self,
        *,
        worker_id: str,
        now: datetime,
        lease_duration: timedelta,
    ) -> WorkClaim | None:
        self.claim_calls += 1
        if self.claim_calls == 1:
            raise RuntimeError("存储暂时不可用")
        return await super().claim_next(
            worker_id=worker_id,
            now=now,
            lease_duration=lease_duration,
        )


class StopAfterSuccessExecutor:
    """成功执行后通知 run_forever 退出。"""

    def __init__(self, stop_event: asyncio.Event) -> None:
        self._stop_event = stop_event
        self.executed = asyncio.Event()

    async def execute(self, task: Task) -> JsonValue:
        self.executed.set()
        self._stop_event.set()
        return {"task_id": str(task.id)}


def _settings(
    worker_id: str,
    *,
    max_attempts: int = 3,
) -> WorkerSettings:
    return WorkerSettings(
        worker_id=worker_id,
        poll_interval_seconds=0.01,
        lease_duration_seconds=10,
        task_timeout_seconds=1,
        retry_policy=RetryPolicy(
            max_attempts=max_attempts,
            base_delay_seconds=2,
            max_delay_seconds=10,
            jitter_ratio=0,
        ),
    )


def _task_at(clock: MutableClock) -> Task:
    return Task(
        payload={"numbers": [1]},
        created_at=clock.current,
        updated_at=clock.current,
    )


def test_worker_settings_require_valid_identity_and_lease() -> None:
    with pytest.raises(ValueError, match="worker_id"):
        WorkerSettings(worker_id="")
    with pytest.raises(ValueError, match="128"):
        WorkerSettings(worker_id="w" * 129)
    with pytest.raises(ValueError, match="lease_duration_seconds"):
        WorkerSettings(worker_id="worker", lease_duration_seconds=1, task_timeout_seconds=1)
    with pytest.raises(ValueError, match="task_timeout_seconds"):
        WorkerSettings(worker_id="worker", task_timeout_seconds=float("nan"))
    with pytest.raises(ValueError, match="lease_duration_seconds"):
        WorkerSettings(worker_id="worker", lease_duration_seconds=float("inf"))


def test_retry_policy_validation_and_exponential_jitter() -> None:
    with pytest.raises(ValueError, match="max_attempts"):
        RetryPolicy(max_attempts=0)
    with pytest.raises(ValueError, match="jitter_ratio"):
        RetryPolicy(jitter_ratio=1.1)
    with pytest.raises(ValueError, match="base_delay_seconds"):
        RetryPolicy(base_delay_seconds=float("nan"))
    with pytest.raises(ValueError, match="max_delay_seconds"):
        RetryPolicy(max_delay_seconds=float("inf"))

    policy = RetryPolicy(
        max_attempts=5,
        base_delay_seconds=2,
        max_delay_seconds=10,
        jitter_ratio=0.25,
    )
    assert calculate_retry_delay(policy, attempt_number=1, random_value=0) == 1.5
    assert calculate_retry_delay(policy, attempt_number=1, random_value=1) == 2.5
    assert calculate_retry_delay(policy, attempt_number=4, random_value=0) == 7.5
    assert calculate_retry_delay(policy, attempt_number=4, random_value=1) == 10

    large_policy = RetryPolicy(
        max_attempts=2000,
        base_delay_seconds=1,
        max_delay_seconds=30,
        jitter_ratio=0,
    )
    assert calculate_retry_delay(large_policy, attempt_number=1025, random_value=0.5) == 30


def test_two_workers_cannot_claim_the_same_task() -> None:
    for iteration in range(20):
        repository = InMemoryTaskRepository()
        clock = MutableClock(datetime(2026, 8, 10, tzinfo=UTC))
        task = _task_at(clock)
        asyncio.run(repository.add(task))
        executor = ThreadSafeSuccessfulExecutor()
        workers = (
            TaskWorker(repository, executor, _settings(f"worker-a-{iteration}"), clock=clock),
            TaskWorker(repository, executor, _settings(f"worker-b-{iteration}"), clock=clock),
        )
        start = Barrier(2)

        def run(worker: TaskWorker, *, barrier: Barrier = start) -> bool:
            barrier.wait(timeout=1)
            return asyncio.run(worker.run_once())

        with ThreadPoolExecutor(max_workers=2) as pool:
            processed = list(pool.map(run, workers))

        stored = asyncio.run(repository.get(task.id))
        attempts = asyncio.run(repository.list_attempts(task.id))
        assert sorted(processed) == [False, True]
        assert executor.calls == 1
        assert stored is not None
        assert stored.status is TaskStatus.SUCCEEDED
        assert len(attempts) == 1
        assert attempts[0].outcome is AttemptOutcome.SUCCEEDED


def test_retryable_failure_uses_backoff_then_succeeds() -> None:
    async def scenario() -> None:
        repository = InMemoryTaskRepository()
        clock = MutableClock(datetime(2026, 8, 10, tzinfo=UTC))
        task = _task_at(clock)
        await repository.add(task)
        executor = RetryTwiceExecutor()
        worker = TaskWorker(
            repository,
            executor,
            _settings("worker-retry"),
            clock=clock,
            random_source=lambda: 0.5,
        )

        assert await worker.run_once() is True
        first_attempts = await repository.list_attempts(task.id)
        assert first_attempts[0].retry_at == clock.current + timedelta(seconds=2)
        assert await worker.run_once() is False

        clock.advance(2)
        assert await worker.run_once() is True
        second_attempts = await repository.list_attempts(task.id)
        assert second_attempts[1].retry_at == clock.current + timedelta(seconds=4)

        clock.advance(4)
        assert await worker.run_once() is True
        stored = await repository.get(task.id)
        attempts = await repository.list_attempts(task.id)

        assert stored is not None
        assert stored.status is TaskStatus.SUCCEEDED
        assert stored.result is not None
        assert stored.result.output == {"task_id": str(task.id), "attempts": 3}
        assert [attempt.outcome for attempt in attempts] == [
            AttemptOutcome.RETRY_SCHEDULED,
            AttemptOutcome.RETRY_SCHEDULED,
            AttemptOutcome.SUCCEEDED,
        ]

    asyncio.run(scenario())


def test_retryable_failure_stops_at_max_attempts() -> None:
    async def scenario() -> None:
        repository = InMemoryTaskRepository()
        clock = MutableClock(datetime(2026, 8, 10, tzinfo=UTC))
        task = _task_at(clock)
        await repository.add(task)
        executor = AlwaysRetryableExecutor()
        worker = TaskWorker(
            repository,
            executor,
            _settings("worker-max", max_attempts=3),
            clock=clock,
            random_source=lambda: 0.5,
        )

        assert await worker.run_once() is True
        clock.advance(2)
        assert await worker.run_once() is True
        clock.advance(4)
        assert await worker.run_once() is True
        assert await worker.run_once() is False

        stored = await repository.get(task.id)
        attempts = await repository.list_attempts(task.id)
        assert executor.calls == 3
        assert stored is not None
        assert stored.status is TaskStatus.FAILED
        assert stored.result is not None
        assert stored.result.error == "任务暂时无法执行"
        assert attempts[-1].outcome is AttemptOutcome.RETRIES_EXHAUSTED
        assert attempts[-1].retry_at is None

    asyncio.run(scenario())


def test_execution_timeout_is_retried_automatically() -> None:
    async def scenario() -> None:
        repository = InMemoryTaskRepository()
        clock = MutableClock(datetime(2026, 8, 10, tzinfo=UTC))
        task = _task_at(clock)
        await repository.add(task)
        executor = TimeoutThenSuccessExecutor()
        settings = WorkerSettings(
            worker_id="worker-timeout",
            poll_interval_seconds=0.01,
            lease_duration_seconds=1,
            task_timeout_seconds=0.01,
            retry_policy=RetryPolicy(
                max_attempts=2,
                base_delay_seconds=2,
                max_delay_seconds=2,
                jitter_ratio=0,
            ),
        )
        worker = TaskWorker(repository, executor, settings, clock=clock)

        assert await worker.run_once() is True
        clock.advance(2)
        assert await worker.run_once() is True

        stored = await repository.get(task.id)
        attempts = await repository.list_attempts(task.id)
        assert stored is not None
        assert stored.status is TaskStatus.SUCCEEDED
        assert executor.calls == 2
        assert attempts[0].error == "任务执行超时"
        assert [attempt.outcome for attempt in attempts] == [
            AttemptOutcome.RETRY_SCHEDULED,
            AttemptOutcome.SUCCEEDED,
        ]

    asyncio.run(scenario())


def test_run_forever_survives_temporary_repository_failure() -> None:
    async def scenario() -> None:
        repository = ClaimFailsOnceRepository()
        clock = MutableClock(datetime(2026, 8, 10, tzinfo=UTC))
        task = _task_at(clock)
        await repository.add(task)
        stop_event = asyncio.Event()
        executor = StopAfterSuccessExecutor(stop_event)
        worker = TaskWorker(
            repository,
            executor,
            _settings("worker-resilient"),
            clock=clock,
        )

        await asyncio.wait_for(worker.run_forever(stop_event), timeout=1)

        stored = await repository.get(task.id)
        assert executor.executed.is_set()
        assert repository.claim_calls == 2
        assert stored is not None
        assert stored.status is TaskStatus.SUCCEEDED

    asyncio.run(scenario())


def test_permanent_failure_is_not_retried() -> None:
    async def scenario() -> None:
        repository = InMemoryTaskRepository()
        clock = MutableClock(datetime(2026, 8, 10, tzinfo=UTC))
        task = _task_at(clock)
        await repository.add(task)
        executor = PermanentFailureExecutor()
        worker = TaskWorker(repository, executor, _settings("worker-permanent"), clock=clock)

        assert await worker.run_once() is True
        clock.advance(60)
        assert await worker.run_once() is False

        stored = await repository.get(task.id)
        attempts = await repository.list_attempts(task.id)
        assert executor.calls == 1
        assert stored is not None
        assert stored.status is TaskStatus.FAILED
        assert stored.result is not None
        assert stored.result.error == "任务无法执行"
        assert len(attempts) == 1
        assert attempts[0].outcome is AttemptOutcome.PERMANENT_FAILURE

    asyncio.run(scenario())


def test_manual_retry_starts_a_new_automatic_retry_cycle() -> None:
    async def scenario() -> None:
        repository = InMemoryTaskRepository()
        service = TaskService(repository)
        clock = MutableClock(datetime(2026, 8, 10, tzinfo=UTC))
        task = _task_at(clock)
        await repository.add(task)
        executor = AlwaysRetryableExecutor()
        worker = TaskWorker(
            repository,
            executor,
            _settings("worker-manual-retry", max_attempts=1),
            clock=clock,
        )

        assert await worker.run_once() is True
        manually_retried = await service.retry(task.id)
        assert manually_retried.status is TaskStatus.QUEUED
        clock.current = manually_retried.updated_at
        assert await worker.run_once() is True

        attempts = await repository.list_attempts(task.id)
        assert executor.calls == 2
        assert [attempt.attempt_number for attempt in attempts] == [1, 2]
        assert [attempt.attempt_in_cycle for attempt in attempts] == [1, 1]
        assert all(attempt.outcome is AttemptOutcome.RETRIES_EXHAUSTED for attempt in attempts)

    asyncio.run(scenario())


def test_forced_worker_cancellation_is_abandoned_not_completed() -> None:
    async def scenario() -> None:
        repository = InMemoryTaskRepository()
        clock = MutableClock(datetime(2026, 8, 10, tzinfo=UTC))
        task = _task_at(clock)
        await repository.add(task)
        executor = CancellationBlockingExecutor()
        metrics = MetricsRegistry()
        worker = TaskWorker(
            repository,
            executor,
            _settings("worker-force-stopped"),
            clock=clock,
            metrics=metrics,
        )

        running = asyncio.create_task(worker.run_once())
        await asyncio.wait_for(executor.started.wait(), timeout=1)
        running.cancel()
        with pytest.raises(asyncio.CancelledError):
            await running

        stored = await repository.get(task.id)
        attempts = await repository.list_attempts(task.id)
        snapshot = metrics.snapshot()
        assert stored is not None
        assert stored.status is TaskStatus.RUNNING
        assert len(attempts) == 1
        assert attempts[0].outcome is AttemptOutcome.RUNNING
        assert snapshot.started == 1
        assert snapshot.completed == 0

    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(JsonLogFormatter())
    worker_logger = logging.getLogger("teyvatnotes.worker.service")
    original = (
        list(worker_logger.handlers),
        worker_logger.level,
        worker_logger.disabled,
        worker_logger.propagate,
    )
    worker_logger.handlers = [handler]
    worker_logger.setLevel(logging.INFO)
    worker_logger.disabled = False
    worker_logger.propagate = False
    try:
        asyncio.run(scenario())
    finally:
        handlers, level, disabled, propagate = original
        worker_logger.handlers = handlers
        worker_logger.setLevel(level)
        worker_logger.disabled = disabled
        worker_logger.propagate = propagate

    records = [json.loads(line) for line in stream.getvalue().splitlines()]
    assert any(record.get("event") == "task_abandoned" for record in records)
    assert not any(
        record.get("event") == "task_completed" and record.get("task_status") == "cancelled"
        for record in records
    )


def test_expired_lease_is_recovered_after_worker_crash() -> None:
    async def scenario() -> None:
        repository = InMemoryTaskRepository()
        clock = MutableClock(datetime(2026, 8, 10, tzinfo=UTC))
        task = _task_at(clock)
        await repository.add(task)

        abandoned = await repository.claim_next(
            worker_id="worker-crashed",
            now=clock.current,
            lease_duration=timedelta(seconds=5),
        )
        assert abandoned is not None

        clock.advance(5)
        executor = SuccessfulExecutor()
        replacement = TaskWorker(
            repository,
            executor,
            _settings("worker-replacement"),
            clock=clock,
        )
        assert await replacement.run_once() is True

        stored = await repository.get(task.id)
        attempts = await repository.list_attempts(task.id)
        assert stored is not None
        assert stored.status is TaskStatus.SUCCEEDED
        assert executor.calls == 1
        assert [attempt.outcome for attempt in attempts] == [
            AttemptOutcome.LEASE_EXPIRED,
            AttemptOutcome.SUCCEEDED,
        ]
        assert attempts[0].worker_id == "worker-crashed"
        assert attempts[1].worker_id == "worker-replacement"

        with pytest.raises(LeaseOwnershipError):
            await repository.complete_claim(
                abandoned,
                output={"stale": True},
                now=clock.current,
            )

    asyncio.run(scenario())


def test_running_cancellation_wins_over_late_success() -> None:
    async def scenario() -> None:
        repository = InMemoryTaskRepository()
        clock = MutableClock(datetime(2026, 8, 10, tzinfo=UTC))
        task = _task_at(clock)
        await repository.add(task)
        claim = await repository.claim_next(
            worker_id="worker-cancel-race",
            now=clock.current,
            lease_duration=timedelta(seconds=10),
        )
        assert claim is not None

        await repository.transition(task.id, TaskStatus.CANCEL_REQUESTED)
        completed = await repository.complete_claim(
            claim,
            output={"too_late": True},
            now=clock.current,
        )
        attempts = await repository.list_attempts(task.id)

        assert completed.status is TaskStatus.CANCELLED
        assert completed.result is None
        assert attempts[0].outcome is AttemptOutcome.CANCELLED

    asyncio.run(scenario())


def test_api_only_mode_persists_without_embedded_execution() -> None:
    repository = InMemoryTaskRepository()
    application = create_app(repository, execution_mode="api_only")

    with TestClient(application, headers=AUTH_HEADERS) as client:
        response = client.post(
            "/v1/tasks",
            json={
                "task_type": "sum_numbers",
                "payload": {"numbers": [1, 2, 3]},
            },
        )
        queried = client.get(response.headers["Location"])

    assert response.status_code == 201
    assert queried.status_code == 200
    assert queried.json()["status"] == "queued"
    assert application.state.task_runtime is None
    assert application.state.execution_mode == "api_only"


def test_api_entrypoint_does_not_construct_an_embedded_application() -> None:
    assert "app" not in api_package.__all__
    assert not isinstance(getattr(api_package, "app", None), FastAPI)


def test_worker_error_details_are_not_exposed_by_task_api() -> None:
    class SensitiveFailureExecutor:
        async def execute(self, task: Task) -> JsonValue:
            del task
            raise PermanentTaskError("upstream api_key=sk-secret-value")

    async def execute(repository: InMemoryTaskRepository, task: Task) -> None:
        clock = MutableClock(task.created_at)
        worker = TaskWorker(
            repository,
            SensitiveFailureExecutor(),
            _settings("worker-sensitive-error"),
            clock=clock,
        )
        assert await worker.run_once() is True

    repository = InMemoryTaskRepository()
    task = Task(payload={"numbers": [1]})
    asyncio.run(repository.add(task))
    asyncio.run(execute(repository, task))
    application = create_app(repository, execution_mode="api_only")

    with TestClient(application, headers=AUTH_HEADERS) as client:
        response = client.get(f"/v1/tasks/{task.id}")

    assert response.status_code == 200
    assert response.json()["result"]["error"] == "任务无法执行"
    assert "sk-secret-value" not in response.text


def test_api_only_mode_replays_duplicate_idempotency_key() -> None:
    repository = InMemoryTaskRepository()
    application = create_app(repository, execution_mode="api_only")
    headers = {"Idempotency-Key": "week5-create-once"}

    with TestClient(application, headers=AUTH_HEADERS) as client:
        original = client.post(
            "/v1/tasks",
            headers=headers,
            json={
                "task_type": "sum_numbers",
                "payload": {"numbers": [1, 2, 3]},
            },
        )
        replay = client.post(
            "/v1/tasks",
            headers=headers,
            json={
                "task_type": "sum_numbers",
                "payload": {"numbers": [999]},
            },
        )

    assert original.status_code == 201
    assert replay.status_code == 201
    assert replay.json() == original.json()


def test_worker_finalizes_queued_cancellation_from_api_only_mode() -> None:
    async def run_worker(repository: InMemoryTaskRepository, task_id: UUID) -> None:
        clock = MutableClock(datetime.now(UTC) + timedelta(seconds=1))
        worker = TaskWorker(
            repository,
            SuccessfulExecutor(),
            _settings("worker-cancellation"),
            clock=clock,
        )
        assert await worker.run_once() is False
        stored = await repository.get(task_id)
        assert stored is not None
        assert stored.status is TaskStatus.CANCELLED

    repository = InMemoryTaskRepository()
    application = create_app(repository, execution_mode="api_only")
    with TestClient(application, headers=AUTH_HEADERS) as client:
        created = client.post(
            "/v1/tasks",
            json={
                "task_type": "sum_numbers",
                "payload": {"numbers": [1]},
            },
        )
        task_id = UUID(created.json()["id"])
        cancellation = client.post(f"/v1/tasks/{task_id}/cancel")

    assert cancellation.status_code == 200
    assert cancellation.json()["status"] == "cancel_requested"
    asyncio.run(run_worker(repository, task_id))
