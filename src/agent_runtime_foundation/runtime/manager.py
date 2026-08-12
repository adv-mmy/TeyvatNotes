"""基于 asyncio.Queue 的进程内异步任务运行时。"""

from __future__ import annotations

import asyncio
import logging
from contextlib import suppress
from dataclasses import dataclass
from time import perf_counter
from uuid import UUID, uuid4

from agent_runtime_foundation.application import (
    CreateTaskCommand,
    TaskActionConflictError,
    TaskRepository,
    TaskService,
)
from agent_runtime_foundation.domain import (
    InvalidTaskTransition,
    JsonValue,
    Task,
    TaskResult,
    TaskStatus,
)
from agent_runtime_foundation.observability import (
    MetricsRegistry,
    bind_correlation_context,
    current_request_id,
)
from agent_runtime_foundation.runtime.errors import (
    RuntimeNotAcceptingError,
    TaskExecutionError,
)
from agent_runtime_foundation.runtime.executor import TaskExecutor

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class RuntimeSettings:
    """Worker、并发、执行超时和停机宽限期配置。"""

    worker_count: int = 4
    max_concurrency: int = 2
    task_timeout_seconds: float = 30.0
    shutdown_timeout_seconds: float = 5.0

    def __post_init__(self) -> None:
        for name, value in (
            ("worker_count", self.worker_count),
            ("max_concurrency", self.max_concurrency),
            ("task_timeout_seconds", self.task_timeout_seconds),
            ("shutdown_timeout_seconds", self.shutdown_timeout_seconds),
        ):
            if value <= 0:
                raise ValueError(f"{name} 必须大于 0")


@dataclass(frozen=True, slots=True)
class RuntimeSnapshot:
    """用于测试和诊断的无副作用运行时快照。"""

    accepting: bool
    queued: int
    pending: int
    active: int
    workers: int


@dataclass(frozen=True, slots=True)
class _WorkItem:
    """一次独立执行尝试；同一业务任务重试时会得到新的 attempt_id。"""

    task_id: UUID
    attempt_id: UUID
    request_id: str | None


@dataclass(frozen=True, slots=True)
class _ActiveAttempt:
    """正在运行或等待 Semaphore 的执行尝试。"""

    item: _WorkItem
    execution: asyncio.Task[None]


class TaskRuntime:
    """管理任务入队、固定 Worker、并发执行、取消和优雅停机。"""

    def __init__(
        self,
        repository: TaskRepository,
        executor: TaskExecutor,
        settings: RuntimeSettings | None = None,
        metrics: MetricsRegistry | None = None,
    ) -> None:
        self._repository = repository
        self._service = TaskService(repository)
        self._executor = executor
        self._settings = settings or RuntimeSettings()
        self._metrics = metrics or MetricsRegistry()
        self._queue: asyncio.Queue[_WorkItem | None] | None = None
        self._semaphore: asyncio.Semaphore | None = None
        self._state_lock: asyncio.Lock | None = None
        self._workers: list[asyncio.Task[None]] = []
        self._active: dict[UUID, _ActiveAttempt] = {}
        self._pending: dict[UUID, _WorkItem] = {}
        self._accepting = False

    @property
    def snapshot(self) -> RuntimeSnapshot:
        """返回当前队列与协程数量，不暴露内部可变集合。"""

        return RuntimeSnapshot(
            accepting=self._accepting,
            queued=self._queue.qsize() if self._queue is not None else 0,
            pending=len(self._pending),
            active=len(self._active),
            workers=sum(not worker.done() for worker in self._workers),
        )

    async def start(self) -> None:
        """创建本次事件循环专属的队列、信号量和固定 Worker。"""

        if self._workers:
            return

        self._queue = asyncio.Queue()
        self._semaphore = asyncio.Semaphore(self._settings.max_concurrency)
        self._state_lock = asyncio.Lock()
        self._accepting = True
        self._workers = [
            asyncio.create_task(
                self._worker_loop(worker_index),
                name=f"agent-runtime-worker-{worker_index}",
            )
            for worker_index in range(self._settings.worker_count)
        ]

    async def create(self, command: CreateTaskCommand) -> Task:
        """在停止接收任务检查和入队之间保持原子性。"""

        queue, state_lock = self._submission_resources()
        async with state_lock:
            self._ensure_accepting()
            creation = await self._service.create(command)
            if creation.created:
                self._enqueue(queue, creation.task.id)
        return creation.task

    async def retry(self, task_id: UUID) -> Task:
        """把失败任务恢复为 queued，并作为一次新执行重新入队。"""

        queue, state_lock = self._submission_resources()
        async with state_lock:
            self._ensure_accepting()
            task = await self._service.retry(task_id)
            self._enqueue(queue, task.id)
        return task

    async def cancel(self, task_id: UUID) -> Task:
        """记录取消请求，并把取消传播到当前执行协程。"""

        task = await self._service.cancel(task_id)
        for active in tuple(self._active.values()):
            if active.item.task_id == task_id:
                active.execution.cancel()
        return task

    async def wait_until_idle(self, timeout: float | None = None) -> None:
        """等待所有已入队任务调用 task_done，主要供测试和停机使用。"""

        queue = self._queue
        if queue is None:
            return
        if timeout is None:
            await queue.join()
            return
        await asyncio.wait_for(queue.join(), timeout=timeout)

    async def stop(self) -> None:
        """停止接收任务；先等待，超时后显式取消剩余任务并关闭 Worker。"""

        queue = self._queue
        state_lock = self._state_lock
        if queue is None or state_lock is None:
            self._accepting = False
            return

        async with state_lock:
            self._accepting = False

        workers_stopped_normally = False
        try:
            try:
                await self.wait_until_idle(self._settings.shutdown_timeout_seconds)
            except TimeoutError:
                logger.warning("优雅停机超时，开始取消 %d 个未完成 attempt", len(self._pending))
                await self._request_cancel_for_pending_tasks()
                try:
                    await self.wait_until_idle(self._settings.shutdown_timeout_seconds)
                except TimeoutError:
                    logger.error("取消宽限期已耗尽，将强制收敛剩余 Worker 和 attempt")
                else:
                    workers_stopped_normally = True
            else:
                workers_stopped_normally = True

            if workers_stopped_normally:
                for _ in self._workers:
                    queue.put_nowait(None)
                await asyncio.gather(*self._workers)
        finally:
            await self._final_cleanup(queue)

    def _submission_resources(
        self,
    ) -> tuple[asyncio.Queue[_WorkItem | None], asyncio.Lock]:
        queue = self._queue
        state_lock = self._state_lock
        if queue is None or state_lock is None:
            raise RuntimeNotAcceptingError
        return queue, state_lock

    def _enqueue(self, queue: asyncio.Queue[_WorkItem | None], task_id: UUID) -> None:
        item = _WorkItem(
            task_id=task_id,
            attempt_id=uuid4(),
            request_id=current_request_id(),
        )
        self._pending[item.attempt_id] = item
        queue.put_nowait(item)

    def _ensure_accepting(self) -> None:
        if not self._accepting:
            raise RuntimeNotAcceptingError

    async def _worker_loop(self, worker_index: int) -> None:
        queue = self._queue
        if queue is None:
            raise RuntimeError("Worker 启动时队列尚未初始化")

        while True:
            item = await queue.get()
            try:
                if item is None:
                    return
                await self._process(item, worker_index)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.error(
                    "Worker 处理 attempt 时发生内部错误",
                    extra={
                        "event": "worker_iteration_failed",
                        "exception_type": type(exc).__name__,
                    },
                )
                if item is not None:
                    await self._safe_finalize_failure(
                        item.task_id,
                        item.attempt_id,
                        "任务运行时内部错误",
                    )
            finally:
                queue.task_done()

    async def _process(self, item: _WorkItem, worker_index: int) -> None:
        with bind_correlation_context(
            request_id=item.request_id,
            task_id=item.task_id,
            attempt_id=item.attempt_id,
            worker_id=f"embedded-{worker_index}",
            inherit=False,
        ):
            execution = asyncio.create_task(
                self._execute(item),
                name=f"agent-runtime-execution-{item.task_id}-{item.attempt_id}",
            )
            self._active[item.attempt_id] = _ActiveAttempt(item=item, execution=execution)
            try:
                await execution
            except asyncio.CancelledError:
                worker = asyncio.current_task()
                if worker is not None and worker.cancelling():
                    raise
                logger.debug("Worker %d 已完成 attempt 的取消清理", worker_index)
            finally:
                active = self._active.get(item.attempt_id)
                if active is not None and active.execution is execution:
                    self._active.pop(item.attempt_id)
                self._pending.pop(item.attempt_id, None)

    async def _execute(self, item: _WorkItem) -> None:
        semaphore = self._semaphore
        if semaphore is None:
            raise RuntimeError("任务执行时 Semaphore 尚未初始化")

        started_at: float | None = None
        outcome: str | None = None
        failure_type: str | None = None
        try:
            async with semaphore:
                task = await self._start_task(item)
                if task is None:
                    return
                started_at = perf_counter()
                self._metrics.record_started()
                logger.info(
                    "任务开始执行",
                    extra={"event": "task_started", "task_status": TaskStatus.RUNNING.value},
                )
                try:
                    async with asyncio.timeout(self._settings.task_timeout_seconds):
                        output = await self._executor.execute(task)
                except TimeoutError:
                    failure_type = "timeout"
                    final_status = await self._finalize_failure(
                        item.task_id,
                        item.attempt_id,
                        "任务执行超时",
                    )
                    outcome = self._metric_outcome(final_status, default="failed")
                except TaskExecutionError as exc:
                    failure_type = type(exc).__name__
                    logger.warning(
                        "任务发生永久执行错误",
                        extra={"event": "task_failed", "failure_type": failure_type},
                    )
                    final_status = await self._finalize_failure(
                        item.task_id,
                        item.attempt_id,
                        "任务无法执行",
                    )
                    outcome = self._metric_outcome(final_status, default="failed")
                except Exception as exc:
                    failure_type = "unexpected_error"
                    logger.error(
                        "任务执行失败",
                        extra={
                            "event": "task_failed",
                            "failure_type": failure_type,
                            "exception_type": type(exc).__name__,
                        },
                    )
                    final_status = await self._finalize_failure(
                        item.task_id,
                        item.attempt_id,
                        "任务执行失败",
                    )
                    outcome = self._metric_outcome(final_status, default="failed")
                else:
                    final_status = await self._finalize_success(
                        item.task_id,
                        item.attempt_id,
                        output,
                    )
                    outcome = self._metric_outcome(final_status, default="succeeded")
        except asyncio.CancelledError:
            final_status = await self._finalize_cancel(item.task_id, item.attempt_id)
            outcome = (
                self._metric_outcome(final_status, default="cancelled")
                if started_at is not None
                else None
            )
            raise
        finally:
            if started_at is not None:
                if outcome is None:
                    outcome = "failed"
                    failure_type = failure_type or "persistence_error"
                duration = perf_counter() - started_at
                self._metrics.record_completed(
                    outcome=outcome,
                    duration_seconds=duration,
                    failure_type=failure_type,
                )
                logger.info(
                    "任务执行结束",
                    extra={
                        "event": "task_completed",
                        "task_status": outcome,
                        "duration_ms": round(duration * 1000, 3),
                        "failure_type": failure_type,
                    },
                )

    async def _start_task(self, item: _WorkItem) -> Task | None:
        task = await self._repository.get(item.task_id)
        if task is None:
            return None
        if task.status is TaskStatus.CANCEL_REQUESTED:
            await self._finalize_cancel(item.task_id, item.attempt_id)
            return None
        if task.status is not TaskStatus.QUEUED:
            return None

        try:
            return await self._repository.transition(
                item.task_id,
                TaskStatus.RUNNING,
                attempt_id=item.attempt_id,
            )
        except InvalidTaskTransition:
            await self._finalize_cancel(item.task_id, item.attempt_id)
            return None

    async def _finalize_success(
        self,
        task_id: UUID,
        attempt_id: UUID,
        output: JsonValue,
    ) -> TaskStatus | None:
        try:
            task = await self._repository.transition(
                task_id,
                TaskStatus.SUCCEEDED,
                result=TaskResult(output=output),
                attempt_id=attempt_id,
            )
        except InvalidTaskTransition:
            return await self._finalize_cancel(task_id, attempt_id)
        return task.status if task is not None else None

    async def _finalize_failure(
        self,
        task_id: UUID,
        attempt_id: UUID,
        message: str,
    ) -> TaskStatus | None:
        try:
            task = await self._repository.transition(
                task_id,
                TaskStatus.FAILED,
                result=TaskResult(error=message),
                attempt_id=attempt_id,
            )
        except InvalidTaskTransition:
            return await self._finalize_cancel(task_id, attempt_id)
        return task.status if task is not None else None

    async def _safe_finalize_failure(
        self,
        task_id: UUID,
        attempt_id: UUID,
        message: str,
    ) -> None:
        """尽力记录失败，但不让存储故障终止固定 Worker。"""

        try:
            await self._finalize_failure(task_id, attempt_id, message)
        except Exception as exc:
            logger.error(
                "无法持久化 attempt 的失败状态",
                extra={"exception_type": type(exc).__name__},
            )

    async def _finalize_cancel(
        self,
        task_id: UUID,
        attempt_id: UUID | None = None,
    ) -> TaskStatus | None:
        task = await self._repository.get(task_id)
        if task is None or task.status in {
            TaskStatus.SUCCEEDED,
            TaskStatus.FAILED,
            TaskStatus.CANCELLED,
        }:
            return task.status if task is not None else None

        if task.status in {TaskStatus.QUEUED, TaskStatus.RUNNING}:
            try:
                task = await self._service.cancel(task_id)
            except TaskActionConflictError:
                task = await self._repository.get(task_id)
                if task is None:
                    return None

        if task.status is TaskStatus.CANCEL_REQUESTED:
            try:
                task = await self._repository.transition(
                    task_id,
                    TaskStatus.CANCELLED,
                    attempt_id=attempt_id,
                )
            except InvalidTaskTransition:
                logger.debug("任务 %s 已由并发路径完成取消", task_id)
                task = await self._repository.get(task_id)
        return task.status if task is not None else None

    @staticmethod
    def _metric_outcome(status: TaskStatus | None, *, default: str) -> str:
        """根据最终持久化状态修正取消竞态的指标。"""

        if status is TaskStatus.SUCCEEDED:
            return "succeeded"
        if status is TaskStatus.FAILED:
            return "failed"
        if status is TaskStatus.CANCELLED:
            return "cancelled"
        return default

    async def _request_cancel_for_pending_tasks(self) -> None:
        task_ids = {item.task_id for item in self._pending.values()}
        for active in tuple(self._active.values()):
            active.execution.cancel()

        for task_id in task_ids:
            try:
                task = await self._repository.get(task_id)
                if task is not None and task.status in {TaskStatus.QUEUED, TaskStatus.RUNNING}:
                    with suppress(TaskActionConflictError):
                        await self._service.cancel(task_id)
            except Exception as exc:
                logger.error(
                    "停机时无法持久化任务的取消请求",
                    extra={"exception_type": type(exc).__name__},
                )

    async def _safe_finalize_cancel(
        self,
        task_id: UUID,
        attempt_id: UUID | None = None,
    ) -> None:
        """尽力持久化取消，数据库故障时仍继续收敛本地资源。"""

        try:
            await self._finalize_cancel(task_id, attempt_id)
        except Exception as exc:
            logger.error(
                "无法持久化任务的取消状态",
                extra={"exception_type": type(exc).__name__},
            )

    async def _final_cleanup(self, queue: asyncio.Queue[_WorkItem | None]) -> None:
        """无论停机路径是否超时，都收敛协程、队列计数和任务状态。"""

        if self._pending or any(not worker.done() for worker in self._workers):
            await self._request_cancel_for_pending_tasks()

        for active in tuple(self._active.values()):
            active.execution.cancel()
        for worker in self._workers:
            if not worker.done():
                worker.cancel()
        await asyncio.gather(*self._workers, return_exceptions=True)

        while True:
            try:
                item = queue.get_nowait()
            except asyncio.QueueEmpty:
                break
            try:
                if item is not None:
                    await self._safe_finalize_cancel(item.task_id, item.attempt_id)
                    self._pending.pop(item.attempt_id, None)
            finally:
                queue.task_done()

        for item in tuple(self._pending.values()):
            await self._safe_finalize_cancel(item.task_id, item.attempt_id)
        self._pending.clear()
        self._active.clear()
        await queue.join()

        self._workers.clear()
        self._queue = None
        self._semaphore = None
        self._state_lock = None
