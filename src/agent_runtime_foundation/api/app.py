"""FastAPI 应用工厂。"""

import os
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from typing import Literal
from uuid import uuid4

from fastapi import FastAPI, Request, Response

from agent_runtime_foundation.api.error_handlers import register_error_handlers
from agent_runtime_foundation.api.routes.tasks import router as tasks_router
from agent_runtime_foundation.application import (
    PersistedTaskCommands,
    TaskCommandHandler,
    TaskRepository,
    TaskService,
)
from agent_runtime_foundation.database import Database, create_database
from agent_runtime_foundation.repositories import (
    InMemoryTaskRepository,
    PostgresTaskRepository,
)
from agent_runtime_foundation.runtime import (
    DeterministicTaskExecutor,
    RuntimeSettings,
    TaskExecutor,
    TaskRuntime,
)

REQUEST_ID_HEADER = "X-Request-ID"
type ExecutionMode = Literal["embedded", "api_only"]


def create_app(
    repository: TaskRepository | None = None,
    *,
    executor: TaskExecutor | None = None,
    runtime_settings: RuntimeSettings | None = None,
    database_url: str | None = None,
    execution_mode: ExecutionMode = "embedded",
) -> FastAPI:
    """创建可注入仓储、执行器和运行时配置的 FastAPI 应用。"""

    if repository is not None and database_url is not None:
        raise ValueError("repository 和 database_url 不能同时传入")

    database: Database | None = None
    resolved_database_url = database_url or os.getenv("DATABASE_URL")
    if repository is not None:
        task_repository = repository
    elif resolved_database_url is not None:
        database = create_database(resolved_database_url)
        task_repository = PostgresTaskRepository(database.session_factory)
    else:
        task_repository = InMemoryTaskRepository()
    task_runtime: TaskRuntime | None
    task_commands: TaskCommandHandler
    if execution_mode == "embedded":
        task_executor = executor if executor is not None else DeterministicTaskExecutor()
        task_runtime = TaskRuntime(
            repository=task_repository,
            executor=task_executor,
            settings=runtime_settings,
        )
        task_commands = task_runtime
    elif execution_mode == "api_only":
        task_runtime = None
        task_commands = PersistedTaskCommands(TaskService(task_repository))
    else:
        raise ValueError(f"不支持的 execution_mode：{execution_mode}")

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        if task_runtime is not None:
            await task_runtime.start()
        try:
            yield
        finally:
            try:
                if task_runtime is not None:
                    await task_runtime.stop()
            finally:
                if database is not None:
                    await database.dispose()

    application = FastAPI(
        title="Agent Runtime Foundation",
        version="0.5.0",
        lifespan=lifespan,
    )
    application.state.task_repository = task_repository
    application.state.task_runtime = task_runtime
    application.state.task_commands = task_commands
    application.state.execution_mode = execution_mode
    application.state.database = database

    @application.middleware("http")
    async def request_id_middleware(
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        request_id = str(uuid4())
        request.state.request_id = request_id
        response = await call_next(request)
        response.headers[REQUEST_ID_HEADER] = request_id
        return response

    register_error_handlers(application)
    application.include_router(tasks_router, prefix="/v1")
    return application
