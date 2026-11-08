"""真实 PostgreSQL 上的 Worker 中断、租约过期与替代实例恢复演示。"""

from __future__ import annotations

import asyncio
import json
import os
from contextlib import suppress
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy.ext.asyncio import AsyncSession

from teyvatnotes.database import create_database
from teyvatnotes.domain import Task, TaskStatus
from teyvatnotes.repositories import (
    PostgresTaskRepository,
    PostgresWorkerRepository,
)
from teyvatnotes.runtime import DeterministicTaskExecutor
from teyvatnotes.worker import RetryPolicy, TaskWorker, WorkerSettings


@dataclass(slots=True)
class MutableClock:
    """让演示无需真实等待租约时长即可推进数据库时间判断。"""

    now: datetime

    def __call__(self) -> datetime:
        return self.now

    async def database_now(self, _session: AsyncSession) -> datetime:
        """作为仅用于演示的数据库时钟测试钩子。"""

        return self.now


class BlockingExecutor:
    """暴露执行起点，并持续阻塞到 Worker 被强制取消。"""

    def __init__(self) -> None:
        self.started = asyncio.Event()

    async def execute(self, _task: Task) -> int:
        self.started.set()
        await asyncio.Future[None]()
        raise AssertionError("阻塞执行器不应自行结束")


@dataclass(frozen=True, slots=True)
class PostgresRecoveryReport:
    """机器可检查的跨连接池恢复结果。"""

    status_before_recovery: str
    status_after_recovery: str
    attempt_outcomes: tuple[str, ...]
    worker_ids: tuple[str, ...]
    result: int | None
    event_worker_ids: tuple[str, ...]


async def run_postgres_recovery_demo(database_url: str) -> PostgresRecoveryReport:
    """中断首个 Worker，再用新的 Engine/Repository 恢复并完成任务。"""

    clock = MutableClock(datetime.now(UTC))
    first_database = create_database(database_url)
    task_repository = PostgresTaskRepository(first_database.session_factory)
    first_worker_repository = PostgresWorkerRepository(
        first_database.session_factory,
        clock=clock.database_now,
    )
    blocking_executor = BlockingExecutor()
    demo_created_at = datetime(2000, 1, 1, tzinfo=UTC)
    task = Task(
        payload={"numbers": [10, 20, 30]},
        created_at=demo_created_at,
        updated_at=demo_created_at,
    )
    await task_repository.add(task)
    first_worker = TaskWorker(
        first_worker_repository,
        blocking_executor,
        WorkerSettings(
            worker_id="postgres-worker-crashed",
            lease_duration_seconds=5,
            task_timeout_seconds=1,
            retry_policy=RetryPolicy(max_attempts=3),
        ),
        clock=clock,
    )
    running = asyncio.create_task(first_worker.run_once(), name="postgres-crashed-worker")
    await blocking_executor.started.wait()
    running.cancel()
    with suppress(asyncio.CancelledError):
        await running
    before = await task_repository.get(task.id)
    if before is None:
        raise RuntimeError("中断后未找到 PostgreSQL 任务")
    await first_database.dispose()

    clock.now += timedelta(seconds=6)
    replacement_database = create_database(database_url)
    replacement_tasks = PostgresTaskRepository(replacement_database.session_factory)
    replacement_repository = PostgresWorkerRepository(
        replacement_database.session_factory,
        clock=clock.database_now,
    )
    replacement_worker = TaskWorker(
        replacement_repository,
        DeterministicTaskExecutor(),
        WorkerSettings(
            worker_id="postgres-worker-replacement",
            lease_duration_seconds=5,
            task_timeout_seconds=1,
            retry_policy=RetryPolicy(max_attempts=3),
        ),
        clock=clock,
    )
    try:
        # 演示可能在已运行过其他集成场景的专用测试库中重复执行。Worker
        # 仍按真实全局队列领取任务，因此继续有限轮次，直到本演示任务完成。
        after = None
        for _ in range(32):
            if not await replacement_worker.run_once():
                break
            after = await replacement_tasks.get(task.id)
            if after is not None and after.status is TaskStatus.SUCCEEDED:
                break
        attempts = await replacement_repository.list_attempts(task.id)
        events = await replacement_tasks.list_events(task.id, after_id=0, limit=100)
    finally:
        await replacement_database.dispose()

    if after is None or after.status is not TaskStatus.SUCCEEDED:
        raise RuntimeError("替代 Worker 未完成 PostgreSQL 任务")
    if events is None:
        raise RuntimeError("恢复后未找到任务事件")
    event_worker_ids = tuple(
        worker_id
        for event in events
        if isinstance((worker_id := event.payload.get("worker_id")), str)
    )
    result: int | None = None
    if after.result is not None and isinstance(after.result.output, dict):
        total = after.result.output.get("total")
        if isinstance(total, int):
            result = total
    return PostgresRecoveryReport(
        status_before_recovery=before.status.value,
        status_after_recovery=after.status.value,
        attempt_outcomes=tuple(attempt.outcome.value for attempt in attempts),
        worker_ids=tuple(attempt.worker_id for attempt in attempts),
        result=result,
        event_worker_ids=event_worker_ids,
    )


def main() -> None:
    """从 TEST_DATABASE_URL 运行演示，避免误连生产 DATABASE_URL。"""

    database_url = os.getenv("TEST_DATABASE_URL")
    if database_url is None or not database_url.strip():
        raise SystemExit("缺少 TEST_DATABASE_URL；只允许在可丢弃测试库运行")
    report = asyncio.run(run_postgres_recovery_demo(database_url))
    print(json.dumps(asdict(report), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
