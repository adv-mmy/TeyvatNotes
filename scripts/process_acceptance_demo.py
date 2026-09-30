"""本机 API、真实 Worker 进程、SSE 和 PostgreSQL 强杀恢复验收。"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import secrets
import socket
import subprocess
import sys
from contextlib import suppress
from dataclasses import asdict, dataclass
from time import perf_counter
from typing import Any
from uuid import UUID, uuid4

import httpx
import uvicorn
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from agent_runtime_foundation.api import create_app
from agent_runtime_foundation.config import WorkerProcessSettings
from agent_runtime_foundation.database import Base, Database, normalize_database_url
from agent_runtime_foundation.domain import JsonValue, Task, TaskStatus
from agent_runtime_foundation.observability import configure_logging
from agent_runtime_foundation.repositories import PostgresTaskRepository, PostgresWorkerRepository
from agent_runtime_foundation.runtime import DeterministicTaskExecutor
from agent_runtime_foundation.worker import TaskWorker, WorkerSettings
from worker_main import run_worker


@dataclass(frozen=True, slots=True)
class ProcessAcceptanceReport:
    """只包含验收证据，不包含 URL、API Key 或任务参数。"""

    recovered_status: str
    recovered_total: int
    attempt_outcomes: tuple[str, ...]
    killed_process_exit_code: int
    recovery_seconds: float
    sse_event_ids: tuple[int, ...]
    sse_resume_exact: bool
    api_worker_log_correlation: bool
    worker_health_ready: bool
    worker_completed: int
    worker_succeeded: int
    secrets_absent_from_logs: bool
    retained_schema: str


def _demo_database(database_url: str, schema: str) -> Database:
    """每个子进程只访问此次演示的隔离 schema，不消费 public 队列。"""

    if re.fullmatch(r"process_demo_[0-9a-f]{32}", schema) is None:
        raise ValueError("非法演示 schema 名称")
    engine = create_async_engine(
        normalize_database_url(database_url),
        pool_pre_ping=True,
        connect_args={"server_settings": {"search_path": schema}},
    )
    return Database(engine, async_sessionmaker(engine, expire_on_commit=False))


async def _demo_api(database_url: str, schema: str, port: int) -> None:
    """使用正式 API 工厂，依赖指向父进程创建的隔离数据库 schema。"""

    configure_logging()
    database = _demo_database(database_url, schema)
    application = create_app(
        PostgresTaskRepository(database.session_factory), execution_mode="api_only"
    )
    server = uvicorn.Server(
        uvicorn.Config(application, host="127.0.0.1", port=port, log_config=None, access_log=False)
    )
    try:
        await server.serve()
    finally:
        await database.dispose()


class CrashDemoExecutor:
    """只在演示子进程阻塞指定任务；其他队列任务仍使用白名单执行器。"""

    def __init__(self, target_id: UUID) -> None:
        self._target_id = target_id

    async def execute(self, task: Task) -> JsonValue:
        if task.id == self._target_id:
            print(json.dumps({"event": "process_demo_execution_started"}), flush=True)
            await asyncio.Event().wait()
        return await DeterministicTaskExecutor().execute(task)


async def _crash_worker(database_url: str, schema: str, task_id: UUID) -> None:
    """使用真实数据库时钟领取，供父进程在执行开始后强杀。"""

    configure_logging()
    database = _demo_database(database_url, schema)
    worker = TaskWorker(
        PostgresWorkerRepository(database.session_factory),
        CrashDemoExecutor(task_id),
        WorkerSettings(
            worker_id="process-demo-crashed",
            poll_interval_seconds=0.05,
            lease_duration_seconds=4,
            task_timeout_seconds=3,
        ),
    )
    try:
        await worker.run_forever(asyncio.Event())
    finally:
        await database.dispose()


def _free_port() -> int:
    """向操作系统查询可用本机端口，避免占用用户正在运行的服务。"""

    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


async def _start_process(*args: str, environ: dict[str, str]) -> asyncio.subprocess.Process:
    creationflags = 0
    # 显式平台分支让类型检查器在 Linux 上跳过 Windows 专用常量。
    if sys.platform == "win32":
        creationflags = subprocess.CREATE_NO_WINDOW
    return await asyncio.create_subprocess_exec(
        sys.executable,
        *args,
        env=environ,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
        creationflags=creationflags,
    )


async def _collect_output(
    process: asyncio.subprocess.Process,
    lines: list[str],
    execution_started: asyncio.Event | None = None,
) -> None:
    """持续排空子进程 PIPE，避免日志填满后阻塞执行。"""

    if process.stdout is None:
        raise RuntimeError("缺少子进程输出通道")
    async for raw in process.stdout:
        line = raw.decode("utf-8", errors="replace").strip()
        lines.append(line)
        if execution_started is not None:
            with suppress(json.JSONDecodeError):
                record = json.loads(line)
                if (
                    isinstance(record, dict)
                    and record.get("event") == "process_demo_execution_started"
                ):
                    execution_started.set()


async def _stop_process(process: asyncio.subprocess.Process) -> None:
    """仅回收本脚本创建的子进程；Windows terminate 是强制退出。"""

    if process.returncode is None:
        process.terminate()
        try:
            async with asyncio.timeout(10):
                await process.wait()
        except TimeoutError:
            process.kill()
            await process.wait()


async def _wait_ready(client: httpx.AsyncClient, url: str) -> dict[str, Any]:
    async with asyncio.timeout(20):
        while True:
            try:
                response = await client.get(url)
                if response.status_code == 200:
                    return dict(response.json())
            except httpx.HTTPError:
                pass
            # 这是有条件的端口就绪轮询，不用固定等待推断进程已经启动。
            await asyncio.sleep(0.05)


async def _read_sse(
    client: httpx.AsyncClient,
    url: str,
    *,
    after_id: int | None = None,
) -> tuple[int, ...]:
    headers = {} if after_id is None else {"Last-Event-ID": str(after_id)}
    event_ids: list[int] = []
    async with asyncio.timeout(30), client.stream("GET", url, headers=headers) as response:
        response.raise_for_status()
        async for line in response.aiter_lines():
            if line.startswith("id:"):
                event_ids.append(int(line.removeprefix("id:").strip()))
    return tuple(event_ids)


async def _wait_succeeded(repository: PostgresTaskRepository, task_id: UUID) -> Task:
    async with asyncio.timeout(20):
        while True:
            task = await repository.get(task_id)
            if task is not None and task.status is TaskStatus.SUCCEEDED:
                return task
            await asyncio.sleep(0.05)


async def _wait_metrics(client: httpx.AsyncClient, monitor_url: str) -> dict[str, Any]:
    """数据库成功提交先于内存指标，等待真正可观察的完成数。"""

    async with asyncio.timeout(10):
        while True:
            snapshot = (await client.get(f"{monitor_url}/metrics")).json()
            if snapshot["succeeded"] >= 2:
                return dict(snapshot)
            await asyncio.sleep(0.05)


def _json_records(lines: list[str]) -> list[dict[str, Any]]:
    records = []
    for line in lines:
        with suppress(json.JSONDecodeError):
            record = json.loads(line)
            if isinstance(record, dict):
                records.append(record)
    return records


async def run_process_acceptance_demo(database_url: str) -> ProcessAcceptanceReport:
    """在专用测试库创建随机 schema 验证真实进程，保留数据便于复核。"""

    api_port, monitor_port = _free_port(), _free_port()
    while monitor_port == api_port:
        monitor_port = _free_port()
    api_key = secrets.token_urlsafe(32)
    environ = dict(os.environ)
    environ.update(
        DATABASE_URL=database_url,
        AGENT_RUNTIME_API_KEY=api_key,
        PYTHONIOENCODING="utf-8",
        PYTHONUNBUFFERED="1",
        AGENT_RUNTIME_WORKER_ID="process-demo-replacement",
        AGENT_RUNTIME_WORKER_MONITOR_HOST="127.0.0.1",
        AGENT_RUNTIME_WORKER_MONITOR_PORT=str(monitor_port),
        AGENT_RUNTIME_WORKER_LEASE_DURATION_SECONDS="4",
        AGENT_RUNTIME_TASK_TIMEOUT_SECONDS="3",
        AGENT_RUNTIME_WORKER_POLL_INTERVAL_SECONDS="0.05",
    )
    schema = f"process_demo_{uuid4().hex}"
    database = _demo_database(database_url, schema)
    repository = PostgresTaskRepository(database.session_factory)
    attempts_repository = PostgresWorkerRepository(database.session_factory)
    processes: list[asyncio.subprocess.Process] = []
    collectors: list[asyncio.Task[None]] = []
    api_lines: list[str] = []
    worker_lines: list[str] = []
    sse_task: asyncio.Task[tuple[int, ...]] | None = None
    api_url = f"http://127.0.0.1:{api_port}"
    monitor_url = f"http://127.0.0.1:{monitor_port}"
    try:
        async with database.engine.begin() as connection:
            # schema 名称仅由本脚本 UUID 生成且已通过固定格式校验。
            await connection.execute(text(f'CREATE SCHEMA "{schema}"'))
            await connection.run_sync(Base.metadata.create_all)
        if not await repository.is_ready():
            raise RuntimeError("演示数据库未就绪")
        api = await _start_process(
            "-m",
            "scripts.process_acceptance_demo",
            "--role",
            "api",
            "--schema",
            schema,
            "--api-port",
            str(api_port),
            environ=environ,
        )
        processes.append(api)
        collectors.append(asyncio.create_task(_collect_output(api, api_lines)))
        async with httpx.AsyncClient(timeout=5) as client:
            await _wait_ready(client, f"{api_url}/health/ready")
            created = await client.post(
                f"{api_url}/v1/tasks",
                headers={"Authorization": f"Bearer {api_key}"},
                json={"task_type": "sum_numbers", "payload": {"numbers": [10, 20, 30]}},
            )
            created.raise_for_status()
            task_id = UUID(created.json()["id"])
            request_id = created.headers["X-Request-ID"]
            sse_url = f"{api_url}/v1/tasks/{task_id}/events"
            sse_task = asyncio.create_task(_read_sse(client, sse_url))
            execution_started = asyncio.Event()
            crashed = await _start_process(
                "-m",
                "scripts.process_acceptance_demo",
                "--schema",
                schema,
                "--crash-task",
                str(task_id),
                environ=environ,
            )
            processes.append(crashed)
            collectors.append(
                asyncio.create_task(_collect_output(crashed, worker_lines, execution_started))
            )
            async with asyncio.timeout(20):
                await execution_started.wait()
            crashed.kill()
            await crashed.wait()
            if crashed.returncode is None or crashed.returncode == 0:
                raise RuntimeError("崩溃 Worker 未以异常退出码结束")
            before = await repository.get(task_id)
            if before is None or before.status is not TaskStatus.RUNNING:
                raise RuntimeError("强杀后任务未保留 running 状态")
            recovery_started = perf_counter()
            replacement = await _start_process(
                "-m",
                "scripts.process_acceptance_demo",
                "--role",
                "worker",
                "--schema",
                schema,
                environ=environ,
            )
            processes.append(replacement)
            collectors.append(asyncio.create_task(_collect_output(replacement, worker_lines)))
            health = await _wait_ready(client, f"{monitor_url}/health/ready")
            recovered = await _wait_succeeded(repository, task_id)
            recovery_seconds = perf_counter() - recovery_started
            sse_ids = await sse_task
            resumed_ids = await _read_sse(client, sse_url, after_id=sse_ids[0])
            normal = await client.post(
                f"{api_url}/v1/tasks",
                headers={"Authorization": f"Bearer {api_key}"},
                json={"task_type": "word_count", "payload": {"text": "one two three"}},
            )
            normal.raise_for_status()
            await _wait_succeeded(repository, UUID(normal.json()["id"]))
            snapshot = await _wait_metrics(client, monitor_url)
            attempts = await attempts_repository.list_attempts(task_id)
        # 进程退出后排空最后的任务完成日志，避免只采到 HTTP 返回前的记录。
        for process in reversed(processes):
            await _stop_process(process)
        await asyncio.gather(*collectors)
        worker_records = _json_records(worker_lines)
        api_records = _json_records(api_lines)
        correlation = any(record.get("request_id") == request_id for record in api_records) and any(
            record.get("request_id") == request_id
            and record.get("task_id") == str(task_id)
            and record.get("worker_id") == "process-demo-replacement"
            for record in worker_records
        )
        output = recovered.result.output if recovered.result is not None else None
        total = output.get("total") if isinstance(output, dict) else None
        outcomes = tuple(attempt.outcome.value for attempt in attempts)
        if total != 60 or type(total) is not int:
            raise RuntimeError("恢复结果不符合求和契约")
        if outcomes != ("lease_expired", "succeeded"):
            raise RuntimeError("恢复 attempt 历史不符合租约契约")
        if not sse_ids or tuple(sorted(set(sse_ids))) != sse_ids or resumed_ids != sse_ids[1:]:
            raise RuntimeError("SSE 游标存在重复或遗漏")
        if not correlation or snapshot["succeeded"] < 2:
            raise RuntimeError("日志关联或 Worker 指标验收失败")
        combined_logs = "\n".join(api_lines + worker_lines)
        password = make_url(normalize_database_url(database_url)).password
        secret_free = (
            api_key not in combined_logs
            and database_url not in combined_logs
            and (not password or password not in combined_logs)
        )
        if not secret_free:
            raise RuntimeError("子进程日志中出现凭据")
        return ProcessAcceptanceReport(
            recovered_status=recovered.status.value,
            recovered_total=total,
            attempt_outcomes=outcomes,
            killed_process_exit_code=crashed.returncode,
            recovery_seconds=round(recovery_seconds, 3),
            sse_event_ids=sse_ids,
            sse_resume_exact=resumed_ids == sse_ids[1:],
            api_worker_log_correlation=correlation,
            worker_health_ready=health["status"] == "ok",
            worker_completed=int(snapshot["completed"]),
            worker_succeeded=int(snapshot["succeeded"]),
            secrets_absent_from_logs=secret_free,
            retained_schema=schema,
        )
    finally:
        if sse_task is not None and not sse_task.done():
            sse_task.cancel()
            with suppress(asyncio.CancelledError):
                await sse_task
        for process in reversed(processes):
            await _stop_process(process)
        await asyncio.gather(*collectors, return_exceptions=True)
        await database.dispose()


def main() -> None:
    """只接受 TEST_DATABASE_URL；父子进程失败时不打印凭据或异常原文。"""

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--crash-task", type=UUID, help=argparse.SUPPRESS)
    parser.add_argument("--role", choices=["api", "worker"], help=argparse.SUPPRESS)
    parser.add_argument("--schema", help=argparse.SUPPRESS)
    parser.add_argument("--api-port", type=int, help=argparse.SUPPRESS)
    args = parser.parse_args()
    database_url = os.getenv("TEST_DATABASE_URL")
    if not database_url:
        raise SystemExit("请设置专用测试库 TEST_DATABASE_URL；测试用户需要 CREATE schema 权限")
    try:
        if args.crash_task is not None:
            asyncio.run(_crash_worker(database_url, args.schema, args.crash_task))
        elif args.role == "api":
            asyncio.run(_demo_api(database_url, args.schema, args.api_port))
        elif args.role == "worker":
            configure_logging()
            asyncio.run(
                run_worker(
                    WorkerProcessSettings.from_env(),
                    stop_event=asyncio.Event(),
                    database=_demo_database(database_url, args.schema),
                )
            )
        else:
            report = asyncio.run(run_process_acceptance_demo(database_url))
            print(json.dumps(asdict(report), ensure_ascii=False, indent=2))
    except Exception as exc:
        raise SystemExit(f"独立进程验收失败：{type(exc).__name__}") from None


if __name__ == "__main__":
    main()
