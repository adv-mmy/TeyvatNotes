"""运行可重复、无脆弱耗时门槛的进程内并发基线。"""

from __future__ import annotations

import argparse
import asyncio
import json
from dataclasses import asdict, dataclass
from time import perf_counter

from agent_runtime_foundation.application import CreateTaskCommand
from agent_runtime_foundation.domain import JsonValue, Task, TaskStatus, TaskType
from agent_runtime_foundation.repositories import InMemoryTaskRepository
from agent_runtime_foundation.runtime import (
    DeterministicTaskExecutor,
    RuntimeSettings,
    TaskRuntime,
)


@dataclass(frozen=True, slots=True)
class ConcurrencyBaselineResult:
    """一次基线执行的输入规模、正确性结果和观测值。"""

    task_count: int
    max_concurrency: int
    peak_concurrency: int
    succeeded: int
    duration_seconds: float
    throughput_per_second: float


class CooperativeBaselineExecutor:
    """在真实 await 点记录并发峰值，再调用项目确定性执行器。"""

    def __init__(self) -> None:
        self.active = 0
        self.peak = 0
        self._deterministic = DeterministicTaskExecutor()

    async def execute(self, task: Task) -> JsonValue:
        self.active += 1
        self.peak = max(self.peak, self.active)
        try:
            await asyncio.sleep(0)
            return await self._deterministic.execute(task)
        finally:
            self.active -= 1


async def run_concurrency_baseline(
    *,
    task_count: int = 200,
    max_concurrency: int = 20,
) -> ConcurrencyBaselineResult:
    """并发提交任务并等待收敛；吞吐量只记录，不设易抖动阈值。"""

    if task_count < 1:
        raise ValueError("task_count 必须大于 0")
    if max_concurrency < 1:
        raise ValueError("max_concurrency 必须大于 0")

    repository = InMemoryTaskRepository()
    executor = CooperativeBaselineExecutor()
    runtime = TaskRuntime(
        repository,
        executor,
        RuntimeSettings(
            worker_count=min(task_count, max_concurrency * 2),
            max_concurrency=max_concurrency,
            task_timeout_seconds=5,
            shutdown_timeout_seconds=5,
        ),
    )
    await runtime.start()
    started_at = perf_counter()
    try:
        commands = [
            CreateTaskCommand(
                task_type=TaskType.SUM_NUMBERS,
                payload={"numbers": [1, 2, 3]},
                idempotency_key=f"baseline-{index}",
            )
            for index in range(task_count)
        ]
        await asyncio.gather(*(runtime.create(command) for command in commands))
        await runtime.wait_until_idle(timeout=10)
        _, succeeded = await repository.list_tasks(
            status=TaskStatus.SUCCEEDED,
            offset=0,
            limit=task_count,
        )
        duration_seconds = perf_counter() - started_at
        if succeeded != task_count:
            raise RuntimeError(f"并发基线未收敛：期望 {task_count}，实际 {succeeded}")
        expected_peak = min(task_count, max_concurrency)
        if executor.peak != expected_peak:
            raise RuntimeError(
                f"并发门控不符合预期：期望峰值 {expected_peak}，实际 {executor.peak}"
            )
        return ConcurrencyBaselineResult(
            task_count=task_count,
            max_concurrency=max_concurrency,
            peak_concurrency=executor.peak,
            succeeded=succeeded,
            duration_seconds=duration_seconds,
            throughput_per_second=succeeded / duration_seconds,
        )
    finally:
        await runtime.stop()


def _positive_int(value: str) -> int:
    parsed = int(value)
    if parsed < 1:
        raise argparse.ArgumentTypeError("必须是大于 0 的整数")
    return parsed


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="运行 Agent Runtime 进程内并发基线")
    parser.add_argument("--tasks", type=_positive_int, default=200, help="提交任务数")
    parser.add_argument(
        "--concurrency",
        type=_positive_int,
        default=20,
        help="Worker 与执行并发上限",
    )
    return parser.parse_args()


def main() -> None:
    """CLI 入口：以便 CI 保存结构化基线输出。"""

    args = _parse_args()
    result = asyncio.run(
        run_concurrency_baseline(
            task_count=args.tasks,
            max_concurrency=args.concurrency,
        )
    )
    print(json.dumps(asdict(result), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
