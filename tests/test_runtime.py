"""第三周异步任务运行时验收测试。"""

import asyncio
from collections.abc import Awaitable, Callable
from uuid import UUID

import httpx
import pytest

from agent_runtime_foundation.api import create_app
from agent_runtime_foundation.application import CreateTaskCommand
from agent_runtime_foundation.domain import JsonValue, Task, TaskResult, TaskStatus, TaskType
from agent_runtime_foundation.repositories import InMemoryTaskRepository
from agent_runtime_foundation.runtime import (
    RuntimeNotAcceptingError,
    RuntimeSettings,
    TaskExecutionError,
    TaskRuntime,
)


def _sum_command(number: int) -> CreateTaskCommand:
    return CreateTaskCommand(
        task_type=TaskType.SUM_NUMBERS,
        payload={"numbers": [number]},
    )


async def _wait_until(
    condition: Callable[[], bool],
    *,
    timeout: float = 1.0,
) -> None:
    async with asyncio.timeout(timeout):
        while not condition():
            await asyncio.sleep(0)


async def _wait_until_async(
    condition: Callable[[], Awaitable[bool]],
    *,
    timeout: float = 1.0,
) -> None:
    async with asyncio.timeout(timeout):
        while not await condition():
            await asyncio.sleep(0)


class ConcurrencyProbeExecutor:
    """等待统一放行，同时记录真正进入 execute 的并发峰值。"""

    def __init__(self) -> None:
        self.active = 0
        self.peak = 0
        self.started = 0
        self.release = asyncio.Event()

    async def execute(self, task: Task) -> JsonValue:
        self.active += 1
        self.started += 1
        self.peak = max(self.peak, self.active)
        try:
            await self.release.wait()
            return {"task_id": str(task.id)}
        finally:
            self.active -= 1


class GateExecutor:
    """用于观察运行中状态和取消传播的可控慢执行器。"""

    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.cleaned_up = asyncio.Event()

    async def execute(self, task: Task) -> JsonValue:
        self.started.set()
        try:
            await self.release.wait()
            return {"task_id": str(task.id)}
        finally:
            self.cleaned_up.set()


class NeverFinishesExecutor:
    """只有取消才能结束，用于验证任务级超时。"""

    async def execute(self, task: Task) -> JsonValue:
        await asyncio.Event().wait()
        return {"task_id": str(task.id)}


class FailThenGateExecutor:
    """第一次立即失败，第二次等待取消，用于覆盖重试 attempt 重叠。"""

    def __init__(self) -> None:
        self.attempts = 0
        self.second_started = asyncio.Event()

    async def execute(self, task: Task) -> JsonValue:
        self.attempts += 1
        if self.attempts == 1:
            raise TaskExecutionError("第一次执行失败")
        self.second_started.set()
        await asyncio.Event().wait()
        return {"task_id": str(task.id)}


class SlowCancellationExecutor:
    """第一次取消后故意慢清理，迫使 stop 进入最终强制收敛分支。"""

    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.cleanup_started = asyncio.Event()

    async def execute(self, task: Task) -> JsonValue:
        self.started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            self.cleanup_started.set()
            await asyncio.sleep(10)
            raise
        return {"task_id": str(task.id)}


class FailingTransitionRepository(InMemoryTaskRepository):
    """每次状态写入都失败，用于验证 Worker 不被补偿异常击穿。"""

    async def transition(
        self,
        task_id: UUID,
        target_status: TaskStatus,
        *,
        result: TaskResult | None = None,
        attempt_id: UUID | None = None,
    ) -> Task | None:
        del task_id, target_status, result, attempt_id
        raise RuntimeError("模拟数据库写入失败")


class SwitchableFailureRepository(InMemoryTaskRepository):
    """在任务开始后切换为完全不可用，用于覆盖停机故障。"""

    def __init__(self) -> None:
        super().__init__()
        self.unavailable = False

    async def get(self, task_id: UUID) -> Task | None:
        if self.unavailable:
            raise RuntimeError("模拟数据库读取失败")
        return await super().get(task_id)

    async def transition(
        self,
        task_id: UUID,
        target_status: TaskStatus,
        *,
        result: TaskResult | None = None,
        attempt_id: UUID | None = None,
    ) -> Task | None:
        if self.unavailable:
            raise RuntimeError("模拟数据库写入失败")
        return await super().transition(
            task_id,
            target_status,
            result=result,
            attempt_id=attempt_id,
        )


def test_runtime_settings_reject_non_positive_values() -> None:
    with pytest.raises(ValueError, match="worker_count"):
        RuntimeSettings(worker_count=0)
    with pytest.raises(ValueError, match="max_concurrency"):
        RuntimeSettings(max_concurrency=0)
    with pytest.raises(ValueError, match="task_timeout_seconds"):
        RuntimeSettings(task_timeout_seconds=0)
    with pytest.raises(ValueError, match="shutdown_timeout_seconds"):
        RuntimeSettings(shutdown_timeout_seconds=0)


def test_runtime_rejects_create_before_lifespan_with_unified_503() -> None:
    async def scenario() -> None:
        application = create_app()
        transport = httpx.ASGITransport(app=application)
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://testserver",
        ) as client:
            response = await client.post(
                "/v1/tasks",
                json={
                    "task_type": "sum_numbers",
                    "payload": {"numbers": [1]},
                },
            )

        assert response.status_code == 503
        assert response.json()["code"] == "runtime_unavailable"
        assert response.json()["request_id"] == response.headers["X-Request-ID"]

    asyncio.run(scenario())


def test_semaphore_enforces_real_concurrency_limit() -> None:
    async def scenario() -> None:
        repository = InMemoryTaskRepository()
        executor = ConcurrencyProbeExecutor()
        runtime = TaskRuntime(
            repository,
            executor,
            RuntimeSettings(
                worker_count=6,
                max_concurrency=2,
                task_timeout_seconds=1,
                shutdown_timeout_seconds=1,
            ),
        )
        await runtime.start()
        try:
            tasks = [await runtime.create(_sum_command(number)) for number in range(6)]
            await _wait_until(lambda: executor.started == 2)

            assert executor.peak == 2
            assert runtime.snapshot.pending == 6

            executor.release.set()
            await runtime.wait_until_idle(timeout=1)

            assert executor.peak == 2
            stored_tasks = [await repository.get(task.id) for task in tasks]
            assert all(
                stored is not None and stored.status is TaskStatus.SUCCEEDED
                for stored in stored_tasks
            )
        finally:
            executor.release.set()
            await runtime.stop()

    asyncio.run(scenario())


def test_slow_execution_does_not_block_query_api() -> None:
    async def scenario() -> None:
        repository = InMemoryTaskRepository()
        executor = GateExecutor()
        application = create_app(
            repository,
            executor=executor,
            runtime_settings=RuntimeSettings(
                worker_count=2,
                max_concurrency=1,
                task_timeout_seconds=1,
                shutdown_timeout_seconds=1,
            ),
        )
        transport = httpx.ASGITransport(app=application)

        async with (
            application.router.lifespan_context(application),
            httpx.AsyncClient(
                transport=transport,
                base_url="http://testserver",
            ) as client,
        ):
            created = await client.post(
                "/v1/tasks",
                json={
                    "task_type": "sum_numbers",
                    "payload": {"numbers": [1, 2, 3]},
                },
            )
            await asyncio.wait_for(executor.started.wait(), timeout=1)

            queried = await asyncio.wait_for(
                client.get(created.headers["Location"]),
                timeout=0.5,
            )

            assert queried.status_code == 200
            assert queried.json()["status"] == "running"

            executor.release.set()
            runtime = application.state.task_runtime
            await runtime.wait_until_idle(timeout=1)
            completed = await client.get(created.headers["Location"])
            assert completed.json()["status"] == "succeeded"

    asyncio.run(scenario())


def test_task_timeout_records_failed_result() -> None:
    async def scenario() -> None:
        repository = InMemoryTaskRepository()
        runtime = TaskRuntime(
            repository,
            NeverFinishesExecutor(),
            RuntimeSettings(
                worker_count=1,
                max_concurrency=1,
                task_timeout_seconds=0.02,
                shutdown_timeout_seconds=1,
            ),
        )
        await runtime.start()
        try:
            task = await runtime.create(_sum_command(1))
            await runtime.wait_until_idle(timeout=1)

            stored = await repository.get(task.id)
            assert stored is not None
            assert stored.status is TaskStatus.FAILED
            assert stored.result is not None
            assert stored.result.error == "任务执行超时"
        finally:
            await runtime.stop()

    asyncio.run(scenario())


def test_cancel_propagates_and_leaves_no_execution_coroutine() -> None:
    async def scenario() -> None:
        repository = InMemoryTaskRepository()
        executor = GateExecutor()
        runtime = TaskRuntime(
            repository,
            executor,
            RuntimeSettings(
                worker_count=2,
                max_concurrency=1,
                task_timeout_seconds=1,
                shutdown_timeout_seconds=1,
            ),
        )
        await runtime.start()
        task = await runtime.create(_sum_command(1))
        await asyncio.wait_for(executor.started.wait(), timeout=1)

        cancelled = await runtime.cancel(task.id)
        assert cancelled.status is TaskStatus.CANCEL_REQUESTED
        await runtime.wait_until_idle(timeout=1)

        stored = await repository.get(task.id)
        assert stored is not None
        assert stored.status is TaskStatus.CANCELLED
        assert executor.cleaned_up.is_set()
        assert runtime.snapshot.active == 0
        assert runtime.snapshot.pending == 0

        await runtime.stop()
        leaked = [
            running
            for running in asyncio.all_tasks()
            if running is not asyncio.current_task()
            and running.get_name().startswith("agent-runtime-")
        ]
        assert leaked == []

    asyncio.run(scenario())


def test_cancelled_queued_task_is_finalized_when_worker_reaches_it() -> None:
    async def scenario() -> None:
        repository = InMemoryTaskRepository()
        executor = GateExecutor()
        runtime = TaskRuntime(
            repository,
            executor,
            RuntimeSettings(
                worker_count=1,
                max_concurrency=1,
                task_timeout_seconds=1,
                shutdown_timeout_seconds=1,
            ),
        )
        await runtime.start()
        first = await runtime.create(_sum_command(1))
        second = await runtime.create(_sum_command(2))
        await asyncio.wait_for(executor.started.wait(), timeout=1)

        cancellation = await runtime.cancel(second.id)
        assert cancellation.status is TaskStatus.CANCEL_REQUESTED

        executor.release.set()
        await runtime.wait_until_idle(timeout=1)

        stored_first = await repository.get(first.id)
        stored_second = await repository.get(second.id)
        assert stored_first is not None
        assert stored_first.status is TaskStatus.SUCCEEDED
        assert stored_second is not None
        assert stored_second.status is TaskStatus.CANCELLED
        await runtime.stop()

    asyncio.run(scenario())


def test_shutdown_marks_every_accepted_task_instead_of_silently_losing_it() -> None:
    async def scenario() -> None:
        repository = InMemoryTaskRepository()
        executor = GateExecutor()
        runtime = TaskRuntime(
            repository,
            executor,
            RuntimeSettings(
                worker_count=4,
                max_concurrency=1,
                task_timeout_seconds=10,
                shutdown_timeout_seconds=0.05,
            ),
        )
        await runtime.start()
        tasks = [await runtime.create(_sum_command(number)) for number in range(6)]
        await asyncio.wait_for(executor.started.wait(), timeout=1)

        await runtime.stop()

        stored_tasks = [await repository.get(task.id) for task in tasks]
        assert all(stored is not None for stored in stored_tasks)
        assert all(
            stored is not None and stored.status is TaskStatus.CANCELLED for stored in stored_tasks
        )
        assert runtime.snapshot.accepting is False
        assert runtime.snapshot.queued == 0
        assert runtime.snapshot.pending == 0
        assert runtime.snapshot.active == 0
        assert runtime.snapshot.workers == 0

        with pytest.raises(RuntimeNotAcceptingError):
            await runtime.create(_sum_command(99))

    asyncio.run(scenario())


def test_immediate_retry_uses_independent_attempt_tracking() -> None:
    async def scenario() -> None:
        repository = InMemoryTaskRepository()
        executor = FailThenGateExecutor()
        runtime = TaskRuntime(
            repository,
            executor,
            RuntimeSettings(
                worker_count=2,
                max_concurrency=2,
                task_timeout_seconds=1,
                shutdown_timeout_seconds=0.05,
            ),
        )
        await runtime.start()
        task = await runtime.create(_sum_command(1))

        async def task_has_failed() -> bool:
            stored = await repository.get(task.id)
            return stored is not None and stored.status is TaskStatus.FAILED

        await _wait_until_async(task_has_failed)

        retried = await runtime.retry(task.id)
        assert retried.status is TaskStatus.QUEUED
        await asyncio.wait_for(executor.second_started.wait(), timeout=1)
        await _wait_until(lambda: runtime.snapshot.pending == 1 and runtime.snapshot.active == 1)

        assert runtime.snapshot.pending == 1
        assert runtime.snapshot.active == 1

        await runtime.stop()
        stored = await repository.get(task.id)
        assert stored is not None
        assert stored.status is TaskStatus.CANCELLED
        assert runtime.snapshot.pending == 0
        assert runtime.snapshot.active == 0
        assert runtime.snapshot.workers == 0

    asyncio.run(scenario())


def test_second_shutdown_timeout_still_runs_final_cleanup() -> None:
    async def scenario() -> None:
        repository = InMemoryTaskRepository()
        executor = SlowCancellationExecutor()
        runtime = TaskRuntime(
            repository,
            executor,
            RuntimeSettings(
                worker_count=2,
                max_concurrency=1,
                task_timeout_seconds=20,
                shutdown_timeout_seconds=0.01,
            ),
        )
        await runtime.start()
        task = await runtime.create(_sum_command(1))
        await asyncio.wait_for(executor.started.wait(), timeout=1)

        await asyncio.wait_for(runtime.stop(), timeout=1)

        stored = await repository.get(task.id)
        assert stored is not None
        assert stored.status is TaskStatus.CANCELLED
        assert executor.cleanup_started.is_set()
        assert runtime.snapshot.pending == 0
        assert runtime.snapshot.active == 0
        assert runtime.snapshot.workers == 0
        leaked = [
            running
            for running in asyncio.all_tasks()
            if running is not asyncio.current_task()
            and running.get_name().startswith("agent-runtime-")
        ]
        assert leaked == []

    asyncio.run(scenario())


def test_repository_failure_does_not_permanently_stop_worker() -> None:
    async def scenario() -> None:
        repository = FailingTransitionRepository()
        runtime = TaskRuntime(
            repository,
            GateExecutor(),
            RuntimeSettings(
                worker_count=1,
                max_concurrency=1,
                task_timeout_seconds=1,
                shutdown_timeout_seconds=0.05,
            ),
        )
        await runtime.start()

        await runtime.create(_sum_command(1))
        await runtime.wait_until_idle(timeout=1)
        assert runtime.snapshot.workers == 1

        await runtime.create(_sum_command(2))
        await runtime.wait_until_idle(timeout=1)
        assert runtime.snapshot.workers == 1

        await runtime.stop()
        assert runtime.snapshot.pending == 0
        assert runtime.snapshot.active == 0
        assert runtime.snapshot.workers == 0

    asyncio.run(scenario())


def test_repository_outage_during_stop_still_cleans_local_resources() -> None:
    async def scenario() -> None:
        repository = SwitchableFailureRepository()
        executor = GateExecutor()
        runtime = TaskRuntime(
            repository,
            executor,
            RuntimeSettings(
                worker_count=1,
                max_concurrency=1,
                task_timeout_seconds=10,
                shutdown_timeout_seconds=0.01,
            ),
        )
        await runtime.start()
        await runtime.create(_sum_command(1))
        await asyncio.wait_for(executor.started.wait(), timeout=1)
        repository.unavailable = True

        await asyncio.wait_for(runtime.stop(), timeout=1)

        assert executor.cleaned_up.is_set()
        assert runtime.snapshot.accepting is False
        assert runtime.snapshot.queued == 0
        assert runtime.snapshot.pending == 0
        assert runtime.snapshot.active == 0
        assert runtime.snapshot.workers == 0
        leaked = [
            running
            for running in asyncio.all_tasks()
            if running is not asyncio.current_task()
            and running.get_name().startswith("agent-runtime-")
        ]
        assert leaked == []

    asyncio.run(scenario())
