"""FastAPI 应用工厂。"""

from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from uuid import uuid4

from fastapi import FastAPI, Request, Response

from agent_runtime_foundation.api.error_handlers import register_error_handlers
from agent_runtime_foundation.api.routes.tasks import router as tasks_router
from agent_runtime_foundation.application import TaskRepository
from agent_runtime_foundation.repositories import InMemoryTaskRepository
from agent_runtime_foundation.runtime import (
    DeterministicTaskExecutor,
    RuntimeSettings,
    TaskExecutor,
    TaskRuntime,
)

REQUEST_ID_HEADER = "X-Request-ID"


def create_app(
    repository: TaskRepository | None = None,
    *,
    executor: TaskExecutor | None = None,
    runtime_settings: RuntimeSettings | None = None,
) -> FastAPI:
    """创建可注入仓储、执行器和运行时配置的 FastAPI 应用。"""

    task_repository = repository if repository is not None else InMemoryTaskRepository()
    task_executor = executor if executor is not None else DeterministicTaskExecutor()
    task_runtime = TaskRuntime(
        repository=task_repository,
        executor=task_executor,
        settings=runtime_settings,
    )

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        await task_runtime.start()
        try:
            yield
        finally:
            await task_runtime.stop()

    application = FastAPI(
        title="Agent Runtime Foundation",
        version="0.3.0",
        lifespan=lifespan,
    )
    application.state.task_repository = task_repository
    application.state.task_runtime = task_runtime

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


app = create_app()
