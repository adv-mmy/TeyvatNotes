"""独立 PostgreSQL Worker 进程入口。"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress

import uvicorn
from fastapi import FastAPI

from teyvatnotes.api.worker_monitoring import create_worker_monitoring_app
from teyvatnotes.config import WorkerProcessSettings
from teyvatnotes.database import Database, create_database
from teyvatnotes.observability import MetricsRegistry, configure_logging
from teyvatnotes.repositories import PostgresTaskRepository, PostgresWorkerRepository
from teyvatnotes.runtime import DeterministicTaskExecutor
from teyvatnotes.worker import TaskWorker


async def run_worker(
    settings: WorkerProcessSettings,
    *,
    stop_event: asyncio.Event,
    database: Database | None = None,
) -> None:
    """运行 Worker，退出时始终释放 asyncpg 连接池。"""

    database = database or create_database(settings.database_url.get_secret_value())
    metrics = MetricsRegistry()
    worker = TaskWorker(
        PostgresWorkerRepository(database.session_factory),
        DeterministicTaskExecutor(),
        settings.worker,
        metrics=metrics,
    )
    worker_task: asyncio.Task[None] | None = None

    async def stop_worker_loop() -> None:
        stop_event.set()
        if worker_task is None:
            return
        try:
            async with asyncio.timeout(settings.worker.task_timeout_seconds + 5):
                await asyncio.shield(worker_task)
        except TimeoutError:
            worker_task.cancel()
            with suppress(asyncio.CancelledError):
                await worker_task

    @asynccontextmanager
    async def lifespan(_application: FastAPI) -> AsyncIterator[None]:
        nonlocal worker_task
        worker_task = asyncio.create_task(worker.run_forever(stop_event), name="worker-loop")
        # 循环意外结束时同时结束监控服务器，让进程重启策略能够接管。
        worker_task.add_done_callback(lambda _task: setattr(server, "should_exit", True))
        try:
            yield
        finally:
            await stop_worker_loop()

    def worker_running() -> bool:
        return worker_task is not None and not worker_task.done() and not stop_event.is_set()

    application = create_worker_monitoring_app(
        metrics=metrics,
        readiness_probe=PostgresTaskRepository(database.session_factory),
        worker_running=worker_running,
        lifespan=lifespan,
    )
    server = uvicorn.Server(
        uvicorn.Config(
            application,
            host=settings.monitor_host,
            port=settings.monitor_port,
            log_config=None,
            access_log=False,
        )
    )

    async def request_stop() -> None:
        await stop_event.wait()
        server.should_exit = True

    stopper = asyncio.create_task(request_stop(), name="worker-stop-watcher")
    try:
        await server.serve()
    finally:
        stop_event.set()
        stopper.cancel()
        with suppress(asyncio.CancelledError):
            await stopper
        try:
            await stop_worker_loop()
        finally:
            await database.dispose()


def main() -> None:
    """通过 Uvicorn 的 SIGINT/SIGTERM 与 lifespan 优雅停止 Worker。"""

    configure_logging()
    settings = WorkerProcessSettings.from_env()

    asyncio.run(run_worker(settings, stop_event=asyncio.Event()))


if __name__ == "__main__":
    main()
