"""以真实公开边界演示阶段一的七个验收场景。"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import secrets
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from io import StringIO
from threading import Event
from time import monotonic, sleep
from typing import cast

from fastapi.testclient import TestClient
from pydantic import SecretStr

from agent_runtime_foundation.api import create_app
from agent_runtime_foundation.config import AppSettings, RateLimitSettings
from agent_runtime_foundation.domain import JsonValue, Task, TaskStatus, TaskType
from agent_runtime_foundation.observability import JsonLogFormatter, MetricsRegistry
from agent_runtime_foundation.repositories import InMemoryTaskRepository
from agent_runtime_foundation.runtime import (
    DeterministicTaskExecutor,
    RuntimeSettings,
)
from agent_runtime_foundation.worker import (
    RetryPolicy,
    TaskWorker,
    WorkerSettings,
)

_CANCEL_SENTINEL = -999_999


@dataclass(slots=True)
class MutableClock:
    """由演示显式推进的 UTC 时钟，避免等待真实租约到期。"""

    current: datetime

    def __call__(self) -> datetime:
        return self.current

    def advance(self, seconds: float) -> None:
        self.current += timedelta(seconds=seconds)


class DemoApiExecutor:
    """普通任务走确定性执行；哨兵任务阻塞到 Runtime 取消它。"""

    def __init__(self) -> None:
        self.cancel_task_started = Event()
        self._deterministic = DeterministicTaskExecutor()

    async def execute(self, task: Task) -> JsonValue:
        if task.payload.get("numbers") == [_CANCEL_SENTINEL]:
            self.cancel_task_started.set()
            await asyncio.Event().wait()
        return await self._deterministic.execute(task)


class TimeoutThenSuccessExecutor:
    """第一次等待超时、第二次成功，用于演示自动重试。"""

    def __init__(self) -> None:
        self.calls = 0

    async def execute(self, task: Task) -> JsonValue:
        self.calls += 1
        if self.calls == 1:
            await asyncio.Event().wait()
        return {"task_id": str(task.id), "attempts": self.calls}


class CrashBlockingExecutor:
    """确认已进入执行后一直阻塞，直到演示代码强制取消 Worker。"""

    def __init__(self) -> None:
        self.started = asyncio.Event()

    async def execute(self, task: Task) -> JsonValue:
        del task
        self.started.set()
        await asyncio.Event().wait()
        return None


@dataclass(frozen=True, slots=True)
class StageOneDemoReport:
    """七个场景的机器可检查结果。"""

    normal_status: str
    normal_total: int
    normal_task_id: str
    normal_request_id: str
    idempotent_same_task: bool
    cancelled_status: str
    timeout_attempt_outcomes: tuple[str, ...]
    crash_recovery_attempt_outcomes: tuple[str, ...]
    crash_recovery_worker_ids: tuple[str, ...]
    crash_status_before_recovery: str
    crash_attempt_outcome_before_recovery: str
    crash_metrics_completed_before_recovery: int
    sse_event_ids: tuple[int, ...]
    sse_event_types: tuple[str, ...]
    sse_correlation_observed: bool
    log_record_count: int
    log_correlation_observed: bool
    crash_abandoned_log_observed: bool
    false_crash_completion_absent: bool
    secret_absent_from_logs: bool


@dataclass(frozen=True, slots=True)
class ApiScenarioResult:
    """HTTP 与 SSE 场景的内部结构化结果。"""

    normal_status: str
    normal_total: int
    normal_task_id: str
    normal_request_id: str
    idempotent_same_task: bool
    cancelled_status: str
    sse_event_ids: tuple[int, ...]
    sse_event_types: tuple[str, ...]
    sse_correlation_observed: bool


def _wait_for_status(
    client: TestClient,
    location: str,
    expected: TaskStatus,
    *,
    timeout_seconds: float = 2,
) -> dict[str, object]:
    deadline = monotonic() + timeout_seconds
    while monotonic() < deadline:
        response = client.get(location)
        response.raise_for_status()
        body = cast(dict[str, object], response.json())
        if body["status"] == expected:
            return body
        sleep(0.01)
    raise TimeoutError(f"任务未在 {timeout_seconds} 秒内进入 {expected}")


def _demo_settings(api_key: str) -> AppSettings:
    return AppSettings(
        api_key=SecretStr(api_key),
        runtime=RuntimeSettings(
            worker_count=2,
            max_concurrency=2,
            task_timeout_seconds=2,
            shutdown_timeout_seconds=2,
        ),
        rate_limit=RateLimitSettings(requests=100, window_seconds=60),
    )


def _run_api_scenarios(api_key: str) -> ApiScenarioResult:
    repository = InMemoryTaskRepository()
    executor = DemoApiExecutor()
    application = create_app(
        repository,
        settings=_demo_settings(api_key),
        executor=executor,
    )
    headers = {"Authorization": f"Bearer {api_key}"}

    with TestClient(application, headers=headers) as client:
        normal = client.post(
            "/v1/tasks",
            json={"task_type": "sum_numbers", "payload": {"numbers": [1, 2, 3]}},
        )
        normal.raise_for_status()
        normal_location = normal.headers["Location"]
        completed = _wait_for_status(client, normal_location, TaskStatus.SUCCEEDED)
        result = completed["result"]
        if not isinstance(result, dict) or result.get("output") != {"total": 6}:
            raise RuntimeError("正常任务结果不符合预期")

        idempotency_headers = {"Idempotency-Key": "stage-one-demo-idempotency"}
        first = client.post(
            "/v1/tasks",
            headers=idempotency_headers,
            json={"task_type": "word_count", "payload": {"text": "hello reliable runtime"}},
        )
        second = client.post(
            "/v1/tasks",
            headers=idempotency_headers,
            json={"task_type": "word_count", "payload": {"text": "ignored duplicate"}},
        )
        first.raise_for_status()
        second.raise_for_status()
        same_task = first.json()["id"] == second.json()["id"]

        cancelling = client.post(
            "/v1/tasks",
            json={
                "task_type": "sum_numbers",
                "payload": {"numbers": [_CANCEL_SENTINEL]},
            },
        )
        cancelling.raise_for_status()
        if not executor.cancel_task_started.wait(timeout=2):
            raise TimeoutError("取消演示任务没有开始执行")
        cancel_response = client.post(f"{cancelling.headers['Location']}/cancel")
        cancel_response.raise_for_status()
        cancelled = _wait_for_status(
            client,
            cancelling.headers["Location"],
            TaskStatus.CANCELLED,
        )

        events = client.get(f"{normal_location}/events")
        events.raise_for_status()
        event_ids = tuple(
            int(line.removeprefix("id:").strip())
            for line in events.text.splitlines()
            if line.startswith("id:")
        )
        event_types = tuple(
            line.removeprefix("event:").strip()
            for line in events.text.splitlines()
            if line.startswith("event:")
        )
        request_id = normal.headers["X-Request-ID"]
        sse_correlation_observed = request_id in events.text

    return ApiScenarioResult(
        normal_status=str(completed["status"]),
        normal_total=6,
        normal_task_id=str(completed["id"]),
        normal_request_id=request_id,
        idempotent_same_task=same_task,
        cancelled_status=str(cancelled["status"]),
        sse_event_ids=event_ids,
        sse_event_types=event_types,
        sse_correlation_observed=sse_correlation_observed,
    )


def _worker_settings(
    worker_id: str,
    *,
    lease_duration_seconds: float = 1,
    task_timeout_seconds: float = 0.02,
) -> WorkerSettings:
    return WorkerSettings(
        worker_id=worker_id,
        poll_interval_seconds=0.01,
        lease_duration_seconds=lease_duration_seconds,
        task_timeout_seconds=task_timeout_seconds,
        retry_policy=RetryPolicy(
            max_attempts=2,
            base_delay_seconds=1,
            max_delay_seconds=1,
            jitter_ratio=0,
        ),
    )


async def _run_worker_scenarios() -> tuple[
    tuple[str, ...],
    tuple[str, ...],
    tuple[str, ...],
    str,
    str,
    int,
]:
    clock = MutableClock(datetime(2026, 8, 22, tzinfo=UTC))

    timeout_repository = InMemoryTaskRepository()
    timeout_task = Task(
        task_type=TaskType.SUM_NUMBERS,
        payload={"numbers": [1]},
        created_at=clock.current,
        updated_at=clock.current,
    )
    await timeout_repository.add(timeout_task)
    timeout_executor = TimeoutThenSuccessExecutor()
    timeout_worker = TaskWorker(
        timeout_repository,
        timeout_executor,
        _worker_settings("worker-timeout"),
        clock=clock,
    )
    await timeout_worker.run_once()
    clock.advance(1)
    await timeout_worker.run_once()
    timeout_attempts = await timeout_repository.list_attempts(timeout_task.id)

    crash_repository = InMemoryTaskRepository()
    crash_task = Task(
        task_type=TaskType.SUM_NUMBERS,
        payload={"numbers": [1, 2, 3]},
        created_at=clock.current,
        updated_at=clock.current,
    )
    await crash_repository.add(crash_task)
    crash_executor = CrashBlockingExecutor()
    crash_metrics = MetricsRegistry()
    crashed_worker = TaskWorker(
        crash_repository,
        crash_executor,
        _worker_settings(
            "worker-crashed",
            lease_duration_seconds=5,
            task_timeout_seconds=2,
        ),
        clock=clock,
        metrics=crash_metrics,
    )
    crashed_run = asyncio.create_task(
        crashed_worker.run_once(),
        name="stage-one-demo-worker-crashed",
    )
    await asyncio.wait_for(crash_executor.started.wait(), timeout=1)
    crashed_run.cancel()
    try:
        await crashed_run
    except asyncio.CancelledError:
        pass
    else:
        raise RuntimeError("强制结束演示没有传播 Worker 取消")

    task_before_recovery = await crash_repository.get(crash_task.id)
    attempts_before_recovery = await crash_repository.list_attempts(crash_task.id)
    if task_before_recovery is None or len(attempts_before_recovery) != 1:
        raise RuntimeError("强制结束后任务或 attempt 证据不完整")
    clock.advance(5)
    replacement = TaskWorker(
        crash_repository,
        DeterministicTaskExecutor(),
        _worker_settings(
            "worker-replacement",
            lease_duration_seconds=5,
            task_timeout_seconds=2,
        ),
        clock=clock,
    )
    await replacement.run_once()
    crash_attempts = await crash_repository.list_attempts(crash_task.id)
    leaked_demo_tasks = [
        task
        for task in asyncio.all_tasks()
        if task is not asyncio.current_task()
        and not task.done()
        and task.get_name().startswith("stage-one-demo-")
    ]
    if leaked_demo_tasks:
        raise RuntimeError("Worker 恢复演示遗留了 asyncio Task")

    return (
        tuple(attempt.outcome.value for attempt in timeout_attempts),
        tuple(attempt.outcome.value for attempt in crash_attempts),
        tuple(attempt.worker_id for attempt in crash_attempts),
        task_before_recovery.status.value,
        attempts_before_recovery[0].outcome.value,
        crash_metrics.snapshot().completed,
    )


@contextmanager
def _capture_safe_project_logs() -> Iterator[StringIO]:
    """临时安装与生产相同的安全 Formatter，并在演示后恢复日志配置。"""

    project_logger = logging.getLogger("agent_runtime_foundation")
    original_handlers = list(project_logger.handlers)
    original_level = project_logger.level
    original_disabled = project_logger.disabled
    original_propagate = project_logger.propagate
    original_global_disable = logging.root.manager.disable
    child_loggers = tuple(
        logging.getLogger(name)
        for name in (
            "agent_runtime_foundation.api.app",
            "agent_runtime_foundation.runtime.manager",
            "agent_runtime_foundation.worker.service",
        )
    )
    original_children = {
        child.name: (
            list(child.handlers),
            child.level,
            child.disabled,
            child.propagate,
        )
        for child in child_loggers
    }
    output = StringIO()
    handler = logging.StreamHandler(output)
    handler.setFormatter(JsonLogFormatter())
    project_logger.handlers = [handler]
    project_logger.setLevel(logging.INFO)
    project_logger.disabled = False
    project_logger.propagate = False
    logging.disable(logging.NOTSET)
    for child in child_loggers:
        child.handlers = []
        child.setLevel(logging.NOTSET)
        child.disabled = False
        child.propagate = True
    try:
        yield output
    finally:
        for child in child_loggers:
            handlers, level, disabled, propagate = original_children[child.name]
            child.handlers = handlers
            child.setLevel(level)
            child.disabled = disabled
            child.propagate = propagate
        logging.disable(original_global_disable)
        project_logger.handlers = original_handlers
        project_logger.setLevel(original_level)
        project_logger.disabled = original_disabled
        project_logger.propagate = original_propagate


def _parse_log_records(log_output: str) -> tuple[dict[str, object], ...]:
    """把每行 JSON 日志解析为机器可检查的记录。"""

    return tuple(
        cast(dict[str, object], json.loads(line))
        for line in log_output.splitlines()
        if line.strip()
    )


def run_stage_one_demo(*, emit_logs: bool = False) -> StageOneDemoReport:
    """运行全部七个场景；测试可关闭日志，命令行默认展示安全 JSON 日志。"""

    api_key = secrets.token_urlsafe(32)
    with _capture_safe_project_logs() as log_output:
        api_result = _run_api_scenarios(api_key)
        (
            timeout_outcomes,
            crash_outcomes,
            crash_worker_ids,
            crash_status_before_recovery,
            crash_attempt_before_recovery,
            crash_metrics_completed_before_recovery,
        ) = asyncio.run(_run_worker_scenarios())
    log_text = log_output.getvalue()
    log_records = _parse_log_records(log_text)
    log_correlation_observed = any(
        record.get("request_id") == api_result.normal_request_id
        and record.get("task_id") == api_result.normal_task_id
        and isinstance(record.get("attempt_id"), str)
        and isinstance(record.get("worker_id"), str)
        for record in log_records
    )
    crash_abandoned_log_observed = any(
        record.get("worker_id") == "worker-crashed" and record.get("event") == "task_abandoned"
        for record in log_records
    )
    false_crash_completion_absent = not any(
        record.get("worker_id") == "worker-crashed"
        and record.get("event") == "task_completed"
        and record.get("task_status") == "cancelled"
        for record in log_records
    )
    secret_absent_from_logs = (
        api_key not in log_text and "Authorization" not in log_text and "Bearer " not in log_text
    )
    if emit_logs:
        print(log_text, end="")
    return StageOneDemoReport(
        normal_status=api_result.normal_status,
        normal_total=api_result.normal_total,
        normal_task_id=api_result.normal_task_id,
        normal_request_id=api_result.normal_request_id,
        idempotent_same_task=api_result.idempotent_same_task,
        cancelled_status=api_result.cancelled_status,
        timeout_attempt_outcomes=timeout_outcomes,
        crash_recovery_attempt_outcomes=crash_outcomes,
        crash_recovery_worker_ids=crash_worker_ids,
        crash_status_before_recovery=crash_status_before_recovery,
        crash_attempt_outcome_before_recovery=crash_attempt_before_recovery,
        crash_metrics_completed_before_recovery=crash_metrics_completed_before_recovery,
        sse_event_ids=api_result.sse_event_ids,
        sse_event_types=api_result.sse_event_types,
        sse_correlation_observed=api_result.sse_correlation_observed,
        log_record_count=len(log_records),
        log_correlation_observed=log_correlation_observed,
        crash_abandoned_log_observed=crash_abandoned_log_observed,
        false_crash_completion_absent=false_crash_completion_absent,
        secret_absent_from_logs=secret_absent_from_logs,
    )


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="运行阶段一七场景验收演示")
    parser.add_argument(
        "--no-logs",
        action="store_true",
        help="不输出安全 JSON 日志，只输出最终报告",
    )
    return parser.parse_args()


def main() -> None:
    args = _parse_args()
    report = run_stage_one_demo(emit_logs=not args.no_logs)
    print(json.dumps(asdict(report), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
